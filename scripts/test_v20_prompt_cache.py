"""V20 Prompt Cache 控制 不变量验证脚本。

不调真实 API；用纯函数 + fake transport / fake response 验证 cache 注入 +
统计累计 + chain 集成行为。

覆盖（13 项）：

prompt_caching 模块（5）:
1. apply_anthropic_cache_control — system + 最后 3 条非 system 消息打 marker
2. apply_anthropic_cache_control — str content 升级为 [text block + cache_control]
3. apply_anthropic_cache_control — 1h TTL 注入 ttl 字段
4. apply_anthropic_cache_control — 空 messages 直通
5. apply_anthropic_cache_control — 深拷贝（不污染原 list）

transport hook（3）:
6. ChatCompletionsTransport.apply_prompt_cache — identity（不打标记）
7. AnthropicTransport.apply_prompt_cache — 调 apply_anthropic_cache_control
8. AnthropicTransport.convert_messages — 保留 cache_control（升级 list 形式）

extract_cache_stats（2）:
9. AnthropicTransport.extract_cache_stats — 命中时返回 read/creation
10. ChatCompletionsTransport.extract_cache_stats — prompt_tokens_details.cached_tokens

chain 集成（3）:
11. chain.cache_enabled=False — 不调 apply_prompt_cache（messages 原样）
12. chain.cache_enabled=True — Anthropic entry 收到带 cache_control 的 messages
13. chain status — cache_read_total / cache_hit_rate 累计正确
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports.prompt_caching import apply_anthropic_cache_control
from transports.chat_completions import ChatCompletionsTransport
from transports.anthropic import AnthropicTransport
from transports.chain import TransportChain, _ChainEntry
from transports.types import NormalizedResponse, Usage


# ── Fake transport（仿 V19 测试） ─────────────────────────────────────────


@dataclass
class _FakeTransport:
    api_mode: str
    plan: list
    received_messages: list = field(default_factory=list)
    call_count: int = 0

    def call(self, client: Any, **kwargs) -> NormalizedResponse:
        self.received_messages.append(kwargs.get("messages"))
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self.plan):
            raise RuntimeError("plan exhausted")
        item = self.plan[idx]
        if isinstance(item, BaseException):
            raise item
        return item

    def apply_prompt_cache(self, messages, cache_ttl="5m"):
        # 默认 identity — 测 11 用
        return messages


class _FakeAnthropicTransport(_FakeTransport):
    def apply_prompt_cache(self, messages, cache_ttl="5m"):
        # 真实 AnthropicTransport 行为 — 走 apply_anthropic_cache_control
        return apply_anthropic_cache_control(messages, cache_ttl=cache_ttl)


# ── 1. prompt_caching 模块 ────────────────────────────────────────────────


def test_apply_cache_system_and_3() -> None:
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    # system 打 marker（content 升级为 list）
    sys_content = out[0]["content"]
    assert isinstance(sys_content, list), "system should become block list"
    assert sys_content[-1].get("cache_control") == {"type": "ephemeral"}
    # 最后 3 条非 system 都打 marker
    for i in (-1, -2, -3):
        last_block = out[i]["content"][-1]
        assert last_block.get("cache_control") == {"type": "ephemeral"}, f"msg[{i}] should have cache_control"
    # 第 1 条非 system（user q1 = idx 1）不打 marker — 总数 5 条非 system，只标最后 3
    user_q1 = out[1]
    if isinstance(user_q1["content"], list):
        assert "cache_control" not in user_q1["content"][-1], "q1 should not have cache_control"
    else:
        assert "cache_control" not in user_q1


def test_apply_cache_str_to_block() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    out = apply_anthropic_cache_control(msgs)
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0] == {
        "type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}
    }


def test_apply_cache_ttl_1h() -> None:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="1h")
    sys_block = out[0]["content"][-1]
    assert sys_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_apply_cache_empty_messages() -> None:
    out = apply_anthropic_cache_control([])
    assert out == []


def test_apply_cache_deep_copy() -> None:
    msgs = [{"role": "user", "content": "q"}]
    out = apply_anthropic_cache_control(msgs)
    # 原 list 不被污染
    assert msgs[0]["content"] == "q", "original message should not be mutated"
    assert isinstance(out[0]["content"], list)


# ── 2. transport hook ────────────────────────────────────────────────────


def test_chat_completions_apply_prompt_cache_identity() -> None:
    t = ChatCompletionsTransport()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    out = t.apply_prompt_cache(msgs, cache_ttl="5m")
    # identity — 不应打 cache_control
    assert out == msgs
    # 仍是同一个引用（默认 ABC 实现是 identity）
    assert out is msgs


def test_anthropic_apply_prompt_cache_invokes_module() -> None:
    t = AnthropicTransport()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    out = t.apply_prompt_cache(msgs)
    # 应该调 apply_anthropic_cache_control — system 升级为 block list
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][-1].get("cache_control") == {"type": "ephemeral"}


def test_anthropic_convert_messages_preserves_cache_control() -> None:
    """注入 cache_control 后 convert_messages 不能丢失它。"""
    t = AnthropicTransport()
    msgs = apply_anthropic_cache_control([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ])
    system, anthropic_msgs = t.convert_messages(msgs)
    # system 升级为 block list 后 cache_control 应保留
    assert isinstance(system, list)
    assert system[-1].get("cache_control") == {"type": "ephemeral"}
    # user 消息的 cache_control 应保留
    user = anthropic_msgs[0]
    assert isinstance(user["content"], list)
    assert user["content"][-1].get("cache_control") == {"type": "ephemeral"}


# ── 3. extract_cache_stats ───────────────────────────────────────────────


def test_anthropic_extract_cache_stats_hit() -> None:
    t = AnthropicTransport()
    fake_response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=80,
        )
    )
    stats = t.extract_cache_stats(fake_response)
    assert stats == {"cached_tokens": 200, "creation_tokens": 80}


def test_chat_completions_extract_cache_stats() -> None:
    t = ChatCompletionsTransport()
    fake_response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=300,
            prompt_tokens_details=SimpleNamespace(cached_tokens=200),
        )
    )
    stats = t.extract_cache_stats(fake_response)
    assert stats == {"cached_tokens": 200, "creation_tokens": 0}


# ── 4. chain 集成 ────────────────────────────────────────────────────────


def _ok_response(prompt_tokens: int, cached: int = 0, write: int = 0) -> NormalizedResponse:
    return NormalizedResponse(
        content="ok", tool_calls=None, finish_reason="stop",
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=10,
            total_tokens=prompt_tokens + 10,
            cached_tokens=cached,
            cache_creation_tokens=write,
        ),
    )


def _build_chain(transport: _FakeTransport, *, cache_enabled: bool) -> TransportChain:
    entry = _ChainEntry(
        api_mode=transport.api_mode,
        transport=transport,
        client=None,
        model=None,
    )
    return TransportChain(
        [entry],
        max_retries=0,
        cache_enabled=cache_enabled,
        sleep_fn=lambda _: None,
    )


def test_chain_cache_disabled_passes_messages_through() -> None:
    fake = _FakeAnthropicTransport(
        "anthropic_messages", plan=[_ok_response(100, cached=0)],
    )
    chain = _build_chain(fake, cache_enabled=False)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    chain.call(model="m", messages=msgs, tools=None)
    received = fake.received_messages[0]
    # cache_enabled=False 时 transport 收到的 messages 原样不动 — 没有 cache_control
    assert received[0]["content"] == "s", "system content should remain a plain str"
    assert received[1]["content"] == "q", "user content should remain a plain str"


def test_chain_cache_enabled_injects_cache_control() -> None:
    fake = _FakeAnthropicTransport(
        "anthropic_messages", plan=[_ok_response(100, cached=0)],
    )
    chain = _build_chain(fake, cache_enabled=True)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    chain.call(model="m", messages=msgs, tools=None)
    received = fake.received_messages[0]
    # cache_enabled=True → transport 收到的 messages 已升级为 block list 带 cache_control
    assert isinstance(received[0]["content"], list), "system should be upgraded to block list"
    assert received[0]["content"][-1].get("cache_control") == {"type": "ephemeral"}
    # 原始 msgs 不被污染（chain 应该做深拷贝）
    assert msgs[0]["content"] == "s", "original msgs[0] should not be mutated"


def test_chain_status_accumulates_cache_stats() -> None:
    """连续两次调用，第一次写入 80（首次 prefix）+ 0 命中，第二次 0 写入 + 200 命中。"""
    fake = _FakeAnthropicTransport(
        "anthropic_messages",
        plan=[
            _ok_response(prompt_tokens=180, cached=0, write=80),  # round 1: 100 input + 80 write
            _ok_response(prompt_tokens=300, cached=200, write=0),  # round 2: 100 input + 200 read
        ],
    )
    chain = _build_chain(fake, cache_enabled=True)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    chain.call(model="m", messages=msgs, tools=None)
    chain.call(model="m", messages=msgs, tools=None)

    status = chain.status()[0]
    assert status["cache_read"] == 200, f"cache_read should be 200, got {status['cache_read']}"
    assert status["cache_write"] == 80, f"cache_write should be 80, got {status['cache_write']}"
    # uncached = (180-0-80) + (300-200-0) = 100 + 100 = 200
    assert status["cache_uncached"] == 200, f"uncached should be 200, got {status['cache_uncached']}"
    # hit_rate = 200 / (200 + 200) = 0.5
    assert abs(status["cache_hit_rate"] - 0.5) < 1e-6, f"hit_rate should be 0.5, got {status['cache_hit_rate']}"


# ── runner ───────────────────────────────────────────────────────────────


def main() -> int:
    tests = [
        # prompt_caching
        ("test_apply_cache_system_and_3", test_apply_cache_system_and_3),
        ("test_apply_cache_str_to_block", test_apply_cache_str_to_block),
        ("test_apply_cache_ttl_1h", test_apply_cache_ttl_1h),
        ("test_apply_cache_empty_messages", test_apply_cache_empty_messages),
        ("test_apply_cache_deep_copy", test_apply_cache_deep_copy),
        # transport hooks
        ("test_chat_completions_apply_prompt_cache_identity", test_chat_completions_apply_prompt_cache_identity),
        ("test_anthropic_apply_prompt_cache_invokes_module", test_anthropic_apply_prompt_cache_invokes_module),
        ("test_anthropic_convert_messages_preserves_cache_control", test_anthropic_convert_messages_preserves_cache_control),
        # extract_cache_stats
        ("test_anthropic_extract_cache_stats_hit", test_anthropic_extract_cache_stats_hit),
        ("test_chat_completions_extract_cache_stats", test_chat_completions_extract_cache_stats),
        # chain integration
        ("test_chain_cache_disabled_passes_messages_through", test_chain_cache_disabled_passes_messages_through),
        ("test_chain_cache_enabled_injects_cache_control", test_chain_cache_enabled_injects_cache_control),
        ("test_chain_status_accumulates_cache_stats", test_chain_status_accumulates_cache_stats),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
