"""V16 retain 批量 + cadence + 多跳 + 时间衰减行为验证脚本。

V16 三个特性的最小不变量集合，覆盖 client 和 server 两侧。

Client 端（retain_every_n_turns 批量）：
1. N=1 行为不变 — 每轮立即入队
2. N=3 缓冲 — 第 1/2 轮不入队，第 3 轮把 3 条合并入队（payload 含 batch:3 tag）
3. session switch 触发 flush — 缓冲到一半时切 session 旧 buffer 必须落盘
4. shutdown 触发 flush — 缓冲到一半时退出旧 buffer 必须落盘

Server 端（2-hop / decay 单元函数）：
5. _hop_weight 单调下降 — hop=1→1.0, hop=2→HOP_DECAY, hop=3→HOP_DECAY^2
6. _time_weight 半衰期正确 — age=half_life 时返回 0.5，age=0 返回 1.0
7. _decay_blend 关闭/开启行为 — DECAY_ALPHA=0 → 1.0；>0 → 介于 (1-α) 和 1 之间
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.remote_semantic import RemoteSemanticProvider


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


def _make_provider(fake: _FakeClient, **kwargs) -> RemoteSemanticProvider:
    with patch("memory.remote_semantic.httpx.Client", return_value=fake):
        provider = RemoteSemanticProvider(
            base_url="http://fake:8765",
            bank_id="test",
            **kwargs,
        )
    provider._client = fake
    provider.initialize(session_id="s-old")
    return provider


# ─── Client tests ───

def test_1_n1_immediate():
    """N=1：每轮立即入队，与 V12-V15 行为一致。"""
    fake = _FakeClient()
    provider = _make_provider(fake, retain_every_n_turns=1)

    provider.sync_turn("u1", "a1")
    provider.sync_turn("u2", "a2")
    provider._retain_queue.join()

    retain_calls = [c for c in fake.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 2, f"expected 2 retain calls, got {len(retain_calls)}"

    for c in retain_calls:
        tags = c["json"].get("tags", [])
        assert not any(t.startswith("batch:") for t in tags), \
            "N=1 should not emit batch: tag"

    provider.shutdown()
    print("  [PASS] test_1_n1_immediate")


def test_2_n3_buffers_and_flushes():
    """N=3：第 1/2 轮缓冲不入队，第 3 轮把 3 条合并入队。"""
    fake = _FakeClient()
    provider = _make_provider(fake, retain_every_n_turns=3)

    provider.sync_turn("u1", "a1")
    provider.sync_turn("u2", "a2")
    time.sleep(0.05)
    retain_calls = [c for c in fake.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 0, f"turn 1/2 should buffer, got {len(retain_calls)} retain calls"

    provider.sync_turn("u3", "a3")
    provider._retain_queue.join()

    retain_calls = [c for c in fake.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 1, f"turn 3 should flush 1 batched retain, got {len(retain_calls)}"

    payload = retain_calls[0]["json"]
    content = payload["content"]
    assert "u1" in content and "u2" in content and "u3" in content, \
        f"batched content must include all 3 turns, got {content!r}"
    assert "batch:3" in payload["tags"], f"expected batch:3 tag, got {payload['tags']}"

    provider.shutdown()
    print("  [PASS] test_2_n3_buffers_and_flushes")


def test_3_session_switch_flushes_partial_buffer():
    """N=3，仅累积 2 条时切 session：旧 buffer 必须 flush 到旧 session_id。"""
    fake = _FakeClient()
    provider = _make_provider(fake, retain_every_n_turns=3)

    provider.sync_turn("u1", "a1")
    provider.sync_turn("u2", "a2")
    assert len(provider._session_turns) == 2

    provider.on_session_switch("s-new", reset=True)

    retain_calls = [c for c in fake.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 1, \
        f"session switch must flush partial buffer, got {len(retain_calls)} retain calls"

    payload = retain_calls[0]["json"]
    assert payload["document_id"] == "s-old", \
        f"flush must use OLD session_id, got {payload['document_id']!r}"
    assert "u1" in payload["content"] and "u2" in payload["content"]
    assert "batch:2" in payload["tags"]

    assert provider._session_turns == []
    assert provider._turn_counter == 0
    assert provider._session_id == "s-new"

    provider.shutdown()
    print("  [PASS] test_3_session_switch_flushes_partial_buffer")


def test_4_shutdown_flushes_partial_buffer():
    """N=5，累积 2 条后 shutdown：buffer 必须 flush，否则丢数据。"""
    fake = _FakeClient()
    provider = _make_provider(fake, retain_every_n_turns=5)

    provider.sync_turn("u1", "a1")
    provider.sync_turn("u2", "a2")
    assert len(provider._session_turns) == 2

    provider.shutdown()

    retain_calls = [c for c in fake.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 1, \
        f"shutdown must flush partial buffer, got {len(retain_calls)} retain calls"

    payload = retain_calls[0]["json"]
    assert "u1" in payload["content"] and "u2" in payload["content"]
    assert "batch:2" in payload["tags"]

    print("  [PASS] test_4_shutdown_flushes_partial_buffer")


# ─── Server unit tests (no DB needed) ───
#
# 直接 import server 文件，执行其 module-level 数学函数。需要先注入 env 让
# import 时的 float() 不报错，并且把 module 注册成 'mock_memory_server'。

def _import_server():
    """Import scripts/mock_memory_server.py without running lifespan."""
    os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
    os.environ.setdefault("EMBEDDING_API_KEY", "x")
    os.environ.setdefault("EMBEDDING_BASE_URL", "http://x")
    os.environ.setdefault("EMBEDDING_MODEL", "x")
    os.environ.setdefault("EMBEDDING_DIM", "1024")
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "mock_memory_server",
        repo / "scripts" / "mock_memory_server.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_5_hop_weight_monotone():
    """_hop_weight: hop=1→1.0, hop=2→HOP_DECAY, hop=3→HOP_DECAY^2，单调下降。"""
    server = _import_server()
    assert server._hop_weight(1) == 1.0
    assert server._hop_weight(0) == 1.0  # 防御
    w2 = server._hop_weight(2)
    w3 = server._hop_weight(3)
    assert 0 < w3 < w2 < 1.0, f"expected w3 < w2 < 1, got w2={w2} w3={w3}"
    # w3 应近似等于 HOP_DECAY * w2
    assert abs(w3 - server.HOP_DECAY * w2) < 1e-9
    print("  [PASS] test_5_hop_weight_monotone")


def test_6_time_weight_half_life():
    """_time_weight: age=0→1.0, age=half_life→0.5, age=2*half_life→0.25。"""
    server = _import_server()
    half = server.DECAY_HALF_LIFE_DAYS
    assert half > 0, "test requires half_life > 0"

    assert abs(server._time_weight(0.0) - 1.0) < 1e-9
    assert abs(server._time_weight(half) - 0.5) < 1e-9
    assert abs(server._time_weight(2 * half) - 0.25) < 1e-9

    # half_life<=0 → 关闭衰减；通过临时改 module 全局验证
    orig = server.DECAY_HALF_LIFE_DAYS
    try:
        server.DECAY_HALF_LIFE_DAYS = 0.0
        assert server._time_weight(1000.0) == 1.0
    finally:
        server.DECAY_HALF_LIFE_DAYS = orig

    print("  [PASS] test_6_time_weight_half_life")


def test_7_decay_blend_alpha_off_and_on():
    """_decay_blend: alpha=0 → 1.0；alpha>0 → 介于 (1-alpha) 和 1 之间。"""
    server = _import_server()
    orig_alpha = server.DECAY_ALPHA
    orig_half = server.DECAY_HALF_LIFE_DAYS
    try:
        # alpha=0 → 完全关闭
        server.DECAY_ALPHA = 0.0
        assert server._decay_blend(1000.0) == 1.0

        # alpha=0.3, half_life=30, age=30 → time_weight=0.5
        # blend = 0.7 + 0.3 * 0.5 = 0.85
        server.DECAY_ALPHA = 0.3
        server.DECAY_HALF_LIFE_DAYS = 30.0
        assert abs(server._decay_blend(30.0) - 0.85) < 1e-9

        # 老到无穷 → time_weight→0 → blend→(1-alpha)=0.7
        b_inf = server._decay_blend(10000.0)
        assert abs(b_inf - 0.7) < 1e-3
    finally:
        server.DECAY_ALPHA = orig_alpha
        server.DECAY_HALF_LIFE_DAYS = orig_half
    print("  [PASS] test_7_decay_blend_alpha_off_and_on")


# ─── 运行 ───

if __name__ == "__main__":
    print("=" * 60)
    print("  V16 retain batch + cadence + multi-hop + decay — Behavior Tests")
    print("=" * 60)

    tests = [
        test_1_n1_immediate,
        test_2_n3_buffers_and_flushes,
        test_3_session_switch_flushes_partial_buffer,
        test_4_shutdown_flushes_partial_buffer,
        test_5_hop_weight_monotone,
        test_6_time_weight_half_life,
        test_7_decay_blend_alpha_off_and_on,
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
            import traceback
            print(f"  [ERROR] {test.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed != 0:
        sys.exit(1)
    print("  All tests passed!")
