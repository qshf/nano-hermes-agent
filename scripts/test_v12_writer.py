"""V12 异步 writer 行为验证脚本。

验证四个关键不变量：
1. sync_turn 入队即返回（不阻塞主循环 — 即便单 job 极慢）
2. job 严格按入队顺序串行执行（单写者保证）
3. 单 job 异常不杀线程，后续 job 继续被处理
4. shutdown 等待 in-flight job drain 完才返回，且重复 shutdown 幂等
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.remote_semantic import RemoteSemanticProvider


class _FakeResponse:
    def raise_for_status(self) -> None:  # noqa: D401
        return None


class _FakeClient:
    """记录所有 POST 调用，并按 `slow` 控制阻塞时长。"""

    def __init__(self, slow: float = 0.0, fail_on: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self._slow = slow
        self._fail_on = fail_on or set()
        self._lock = threading.Lock()

    def post(self, url: str, json: dict | None = None):
        with self._lock:
            idx = len(self.calls)
            self.calls.append({"url": url, "json": json})
        if self._slow:
            time.sleep(self._slow)
        if idx in self._fail_on:
            raise RuntimeError(f"injected failure on call #{idx}")
        return _FakeResponse()

    def get(self, url: str):
        return _FakeResponse()

    def close(self) -> None:
        pass


def _make_provider(client: _FakeClient) -> RemoteSemanticProvider:
    provider = RemoteSemanticProvider(
        base_url="http://127.0.0.1:9",
        bank_id="test",
        auto_retain=True,
        writer_join_timeout=5.0,
    )
    provider._client = client  # type: ignore[assignment]
    provider.initialize(session_id="s1")
    return provider


def test_enqueue_does_not_block() -> None:
    """每个 retain 都让 server 睡 0.5s — 入队 3 次的总耗时必须 << 1.5s。"""
    client = _FakeClient(slow=0.5)
    provider = _make_provider(client)

    t0 = time.monotonic()
    for i in range(3):
        provider.sync_turn(f"u{i}", f"a{i}")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2, f"sync_turn should be non-blocking, took {elapsed:.3f}s"

    provider.shutdown()
    assert len(client.calls) == 3, f"expected 3 calls drained on shutdown, got {len(client.calls)}"
    print(f"  enqueue_does_not_block OK (enqueue 3x took {elapsed*1000:.1f}ms, drain done)")


def test_order_preserved() -> None:
    """单写者必须按 FIFO 顺序串行执行。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)

    for i in range(20):
        provider.sync_turn(f"u{i}", f"a{i}")
    provider.shutdown()

    expected = [f"User: u{i}\nAssistant: a{i}" for i in range(20)]
    actual = [c["json"]["content"] for c in client.calls]
    assert actual == expected, f"order broken:\nexpected={expected[:3]}...\nactual  ={actual[:3]}..."
    print(f"  order_preserved OK ({len(actual)} jobs in FIFO order)")


def test_single_failure_does_not_kill_writer() -> None:
    """第 2 个 job 抛异常，第 3、4 个仍应被处理。"""
    client = _FakeClient(slow=0.0, fail_on={1})
    provider = _make_provider(client)

    for i in range(4):
        provider.sync_turn(f"u{i}", f"a{i}")
    provider.shutdown()

    assert len(client.calls) == 4, (
        f"writer should survive failure, expected 4 calls, got {len(client.calls)}"
    )
    print(f"  single_failure_does_not_kill_writer OK (4/4 jobs attempted)")


def test_shutdown_drains_inflight() -> None:
    """shutdown 必须等已入队的 slow job drain 完。"""
    client = _FakeClient(slow=0.3)
    provider = _make_provider(client)

    for i in range(3):
        provider.sync_turn(f"u{i}", f"a{i}")
    t0 = time.monotonic()
    provider.shutdown()
    elapsed = time.monotonic() - t0

    assert len(client.calls) == 3, f"shutdown should drain, got {len(client.calls)}/3"
    # 3 个 job × 0.3s 串行 ≈ 0.9s 起步
    assert elapsed >= 0.8, f"shutdown returned too early ({elapsed:.3f}s), did it really drain?"
    print(f"  shutdown_drains_inflight OK (3 slow jobs drained in {elapsed:.3f}s)")


def test_shutdown_is_idempotent() -> None:
    """重复 shutdown 不应抛错，也不应再启动 writer。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)
    provider.sync_turn("u", "a")
    provider.shutdown()
    provider.shutdown()  # 第二次 — 应该是 no-op
    print(f"  shutdown_is_idempotent OK")


def test_disabled_after_shutdown() -> None:
    """shutdown 后 sync_turn 应被丢弃而不是入队（避免给已死 writer 加 job）。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)
    provider.shutdown()

    provider.sync_turn("u", "a")
    assert len(client.calls) == 0, f"expected drop after shutdown, got {len(client.calls)} calls"
    print(f"  disabled_after_shutdown OK")


def test_lazy_writer_start() -> None:
    """从未 retain 过的 provider 不应该挂着 writer 线程。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)
    assert provider._writer_thread is None, "writer should not start until first enqueue"
    provider.sync_turn("u", "a")
    assert provider._writer_thread is not None and provider._writer_thread.is_alive()
    provider.shutdown()
    print(f"  lazy_writer_start OK")


def main() -> int:
    tests = [
        test_enqueue_does_not_block,
        test_order_preserved,
        test_single_failure_does_not_kill_writer,
        test_shutdown_drains_inflight,
        test_shutdown_is_idempotent,
        test_disabled_after_shutdown,
        test_lazy_writer_start,
    ]
    print(f"Running {len(tests)} V12 writer tests...")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
