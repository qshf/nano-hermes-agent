"""V15 上下文压缩行为验证脚本。

验证关键不变量：
1. should_compress 阈值判断（只看 API 真实 prompt_tokens；首轮无值不触发）
2. anti-thrashing — 连续低效压缩后停止
3. Phase 1 — prune old tool results（大 tool output 被替换）
4. Phase 2 — tail boundary by token budget（不在 tool msg 上切割）
5. Phase 3 — LLM 摘要 + iterative update（第二次压缩增量更新）
6. Phase 4 — 压缩后 messages 结构正确（head + summary + tail）
7. Phase 5 — sanitize tool pairs（孤立 result 被删、缺失 result 补 stub）
8. on_pre_compress_all 广播到 provider
9. RemoteSemanticProvider 的 on_pre_compress 入队 retain
10. 真实 token 节省结算（pre 真实 → 下一轮 update_usage 结算 ineffective）
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_compressor import ContextCompressor, SUMMARY_PREFIX, _PRUNED_TOOL_PLACEHOLDER
from memory.remote_semantic import RemoteSemanticProvider
from memory.manager import MemoryManager
from memory.provider import MemoryProvider


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"results": []}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def post(self, url: str, json: dict | None = None):
        with self._lock:
            self.calls.append({"url": url, "json": json})
        return _FakeResponse()

    def get(self, url: str):
        return _FakeResponse()

    def close(self) -> None:
        pass


def _make_messages(n: int, char_per_msg: int = 200) -> list[dict]:
    """生成 n 条测试消息（1 system + n-1 user/assistant 交替）。"""
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(1, n):
        role = "user" if i % 2 == 1 else "assistant"
        msgs.append({"role": role, "content": f"Message {i}: " + "x" * char_per_msg})
    return msgs


def _mock_client(summary_text: str = "## Active Task\nTest task") -> MagicMock:
    mock = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = summary_text
    mock.chat.completions.create.return_value = resp
    return mock


class _FakeTransport:
    """V19+: compressor 走 transport.call(...) 拿摘要。"""
    def __init__(self, summary_text: str = "## Active Task\nTest task") -> None:
        self.summary_text = summary_text
        self.last_messages: list[dict] | None = None

    def call(self, client=None, *, model: str, messages: list[dict], **_):
        self.last_messages = list(messages)
        normalized = MagicMock()
        normalized.content = self.summary_text
        return normalized


def _fake_transport(summary_text: str = "## Active Task\nTest task") -> _FakeTransport:
    return _FakeTransport(summary_text)


# ─── Tests ───


def test_1_should_compress_threshold():
    """触发只看 API 真实 prompt_tokens；首轮没有真实值前不触发。"""
    compressor = ContextCompressor(context_window=2000, threshold_percent=0.5)
    # threshold = 1000 tokens

    short_msgs = _make_messages(8, char_per_msg=50)
    long_msgs = _make_messages(30, char_per_msg=200)

    # 首轮没有 update_usage —— 不论消息长短都不触发
    assert not compressor.should_compress(short_msgs), "no Usage yet → no trigger"
    assert not compressor.should_compress(long_msgs), "no Usage yet → no trigger (even if long)"

    # update_usage 喂入超阈值的真实值 —— 触发
    compressor.update_usage(1200)
    assert compressor.should_compress(short_msgs), "real prompt_tokens above threshold → trigger"

    # update_usage 喂入低于阈值 —— 不触发
    compressor.update_usage(500)
    assert not compressor.should_compress(long_msgs), "real prompt_tokens below threshold → skip"

    print("  [PASS] test_1_should_compress_threshold")


def test_2_anti_thrashing():
    """连续 2 次低效压缩后 should_compress 返回 False。"""
    compressor = ContextCompressor(context_window=2000, threshold_percent=0.5)
    compressor._ineffective_count = 2
    compressor.update_usage(1500)  # 真实值超阈值

    long_msgs = _make_messages(30, char_per_msg=200)
    assert not compressor.should_compress(long_msgs), "Should skip after 2 ineffective compressions"

    # 重置后恢复
    compressor._ineffective_count = 0
    assert compressor.should_compress(long_msgs), "Should compress after reset"

    print("  [PASS] test_2_anti_thrashing")


def test_3_prune_old_tool_results():
    """Phase 1: 大 tool output 被替换为 placeholder。"""
    compressor = ContextCompressor(context_window=10000, tail_token_budget=500)

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "run tests"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "x" * 2000},  # 大 output
        {"role": "assistant", "content": "Tests passed"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "You're welcome!"},
    ]

    pruned = compressor._prune_old_tool_results(messages)

    # 找到被 prune 的 tool message（不在 tail 中的大 output）
    tool_msgs = [m for m in pruned if m.get("role") == "tool"]
    for tm in tool_msgs:
        if tm.get("tool_call_id") == "call_1":
            # 如果在 tail 外，应该被 prune
            idx = pruned.index(tm)
            tail_boundary = compressor._find_tail_boundary_simple(messages)
            if idx < tail_boundary:
                assert tm["content"] == _PRUNED_TOOL_PLACEHOLDER, "Large tool output should be pruned"

    print("  [PASS] test_3_prune_old_tool_results")


def test_4_tail_boundary_avoids_tool():
    """Phase 2: tail 边界不在 tool message 上。"""
    compressor = ContextCompressor(context_window=10000, tail_token_budget=200)

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "a" * 400},
        {"role": "assistant", "content": "b" * 400},
        {"role": "user", "content": "c" * 400},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "bye"},
    ]

    tail_start = compressor._find_tail_boundary(messages, head_end=3)
    # tail 起始不应该是 tool message
    assert messages[tail_start].get("role") != "tool", \
        f"Tail should not start on tool message, got role={messages[tail_start].get('role')} at idx {tail_start}"

    print("  [PASS] test_4_tail_boundary_avoids_tool")


def test_5_iterative_update():
    """Phase 3: 第二次压缩使用 iterative update（prompt 包含 PREVIOUS SUMMARY）。"""
    compressor = ContextCompressor(context_window=2000, threshold_percent=0.3, tail_token_budget=200)

    transport = _fake_transport("## Active Task\nFirst summary")
    messages = _make_messages(20, char_per_msg=100)
    compressor.update_usage(1500)
    compressor.compress(messages, client=None, model="test-model", transport=transport)

    assert compressor._previous_summary == "## Active Task\nFirst summary"

    # 第二次压缩 —— update_usage 既结算上一次 pending，又喂入超阈值新真实值
    transport2 = _fake_transport("## Active Task\nUpdated summary")
    messages2 = _make_messages(20, char_per_msg=100)
    compressor.update_usage(1500)
    compressor.compress(messages2, client=None, model="test-model", transport=transport2)

    prompt_content = transport2.last_messages[0]["content"]
    assert "PREVIOUS SUMMARY" in prompt_content, "Second compression should use iterative update"
    assert "First summary" in prompt_content, "Should include previous summary content"

    print("  [PASS] test_5_iterative_update")


def test_6_compress_structure():
    """Phase 4: 压缩后结构 = head + summary + tail。"""
    compressor = ContextCompressor(context_window=2000, threshold_percent=0.3, tail_token_budget=300)

    messages = _make_messages(20, char_per_msg=100)
    transport = _fake_transport()
    compressor.update_usage(1500)
    result = compressor.compress(messages, client=None, model="test-model", transport=transport)

    # 第一条应该是 system
    assert result[0]["role"] == "system"
    assert result[0]["content"] == messages[0]["content"]

    summary_found = False
    for msg in result:
        if isinstance(msg.get("content"), str) and SUMMARY_PREFIX in msg["content"]:
            summary_found = True
            break
    assert summary_found, "Should contain summary with COMPACTION prefix"

    assert len(result) < len(messages), "Compressed should be shorter"

    print("  [PASS] test_6_compress_structure")


def test_7_sanitize_tool_pairs():
    """Phase 5: 孤立 tool result 被删，缺失 result 补 stub。"""
    compressor = ContextCompressor(context_window=10000)

    # Case 1: 孤立 tool result（对应的 assistant tool_call 已被摘要掉）
    messages_orphan = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "orphan_1", "content": "result"},
        {"role": "user", "content": "hello"},
    ]
    sanitized = compressor._sanitize_tool_pairs(messages_orphan)
    tool_msgs = [m for m in sanitized if m.get("role") == "tool"]
    assert len(tool_msgs) == 0, "Orphaned tool result should be removed"

    # Case 2: assistant 有 tool_call 但 result 缺失
    messages_missing = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_99", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "user", "content": "hello"},
    ]
    sanitized = compressor._sanitize_tool_pairs(messages_missing)
    tool_msgs = [m for m in sanitized if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, "Should insert stub for missing result"
    assert tool_msgs[0]["tool_call_id"] == "call_99"
    assert "earlier" in tool_msgs[0]["content"].lower()

    print("  [PASS] test_7_sanitize_tool_pairs")


def test_8_on_pre_compress_all_broadcasts():
    """on_pre_compress_all 广播到所有 provider。"""

    class _TrackingProvider(MemoryProvider):
        def __init__(self):
            self.pre_compress_calls = []

        @property
        def name(self):
            return "tracking"

        def is_available(self):
            return True

        def initialize(self, session_id="", **kwargs):
            pass

        def get_tool_schemas(self):
            return []

        def on_pre_compress(self, messages, **kwargs):
            self.pre_compress_calls.append(messages)

    manager = MemoryManager()
    p = _TrackingProvider()
    manager.add_provider(p)

    test_messages = [{"role": "user", "content": "hello"}]
    manager.on_pre_compress_all(test_messages)

    assert len(p.pre_compress_calls) == 1
    assert p.pre_compress_calls[0] == test_messages

    print("  [PASS] test_8_on_pre_compress_all_broadcasts")


def test_9_remote_provider_enqueues_retain():
    """RemoteSemanticProvider 的 on_pre_compress 入队 retain。"""
    fake_client = _FakeClient()

    with patch("memory.remote_semantic.httpx.Client", return_value=fake_client):
        provider = RemoteSemanticProvider(
            base_url="http://fake:8765",
            bank_id="test",
        )
        provider._client = fake_client
        provider.initialize(session_id="test-session")

        messages = [
            {"role": "user", "content": "我叫小明"},
            {"role": "assistant", "content": "你好小明！"},
            {"role": "user", "content": "我在做Python项目"},
            {"role": "assistant", "content": "好的，有什么需要帮助的？"},
        ]

        provider.on_pre_compress(messages)
        time.sleep(0.5)

        retain_calls = [c for c in fake_client.calls if "/retain" in c["url"]]
        assert len(retain_calls) == 1, f"Expected 1 retain call, got {len(retain_calls)}"

        payload = retain_calls[0]["json"]
        assert "[Pre-compression context]" in payload["content"]
        assert "pre-compress" in payload["tags"]

        provider.shutdown()

    print("  [PASS] test_9_remote_provider_enqueues_retain")


def test_10_real_token_savings_settlement():
    """真实 token 节省结算：compress 挂起 _pending_pre_tokens，下一轮
    update_usage 用真实 prompt_tokens 计算节省比例 + 更新 ineffective_count。"""
    compressor = ContextCompressor(context_window=2000, threshold_percent=0.3, tail_token_budget=200)

    # 第一次：节省 50%（pre=1500 → post=750）
    transport = _fake_transport()
    messages = _make_messages(20, char_per_msg=100)
    compressor.update_usage(1500)
    assert compressor.should_compress(messages)
    compressor.compress(messages, client=None, model="m", transport=transport)
    assert compressor._pending_pre_tokens == 1500, "compress should stash real pre"
    assert compressor._last_prompt_tokens is None, "compress should clear stale last value"

    compressor.update_usage(750)  # 模拟下一次 API 真实回报
    assert compressor._ineffective_count == 0, "50% savings → effective"
    assert compressor._pending_pre_tokens is None, "settled"
    assert compressor._last_prompt_tokens == 750

    # 第二次：节省 5%（低效）—— 计数 +1
    compressor.update_usage(1600)
    compressor.compress(messages, client=None, model="m", transport=_fake_transport())
    compressor.update_usage(1520)
    assert compressor._ineffective_count == 1, "5% savings → ineffective"

    # 第三次：再次低效 —— 累计到 2，触发 anti-thrashing
    compressor.update_usage(1600)
    compressor.compress(messages, client=None, model="m", transport=_fake_transport())
    compressor.update_usage(1530)
    assert compressor._ineffective_count == 2
    assert not compressor.should_compress(messages), "anti-thrashing kicks in"

    print("  [PASS] test_10_real_token_savings_settlement")


# ─── 运行 ───

if __name__ == "__main__":
    print("=" * 60)
    print("  V15 Context Compression — Behavior Tests (5-Phase Pipeline)")
    print("=" * 60)

    tests = [
        test_1_should_compress_threshold,
        test_2_anti_thrashing,
        test_3_prune_old_tool_results,
        test_4_tail_boundary_avoids_tool,
        test_5_iterative_update,
        test_6_compress_structure,
        test_7_sanitize_tool_pairs,
        test_8_on_pre_compress_all_broadcasts,
        test_9_remote_provider_enqueues_retain,
        test_10_real_token_savings_settlement,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed == 0:
        print("  All tests passed!")
    else:
        sys.exit(1)
