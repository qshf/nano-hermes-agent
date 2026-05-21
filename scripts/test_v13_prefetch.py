"""V13 后台 prefetch 预热行为验证脚本。

验证六个关键不变量：
1. queue_prefetch 启动后台 daemon 线程
2. prefetch 消费缓存结果（不发新 HTTP 调用）
3. join 超时后 fallback 到同步调用
4. shutdown 后 queue_prefetch 是 no-op
5. 冷启动（无预热）时 prefetch 同步 fallback
6. tools 模式下 queue_prefetch 和 prefetch 均跳过
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
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "results": [
                {"text": "用户叫李雷", "score": 0.95, "source": "entity"},
                {"text": "项目是 nano_hermes_agent", "score": 0.88, "source": "fact"},
            ]
        }


class _FakeClient:
    """记录所有请求，可注入延迟。"""

    def __init__(self, slow: float = 0.0) -> None:
        self.calls: list[dict] = []
        self._slow = slow
        self._lock = threading.Lock()

    def post(self, url: str, json: dict | None = None):
        if self._slow:
            time.sleep(self._slow)
        with self._lock:
            self.calls.append({"url": url, "json": json})
        return _FakeResponse()

    def get(self, url: str):
        return _FakeResponse()

    def close(self) -> None:
        pass


def _make_provider(client: _FakeClient, **kwargs) -> RemoteSemanticProvider:
    defaults = {
        "base_url": "http://127.0.0.1:9",
        "bank_id": "test",
        "memory_mode": "hybrid",
        "auto_retain": False,
        "auto_recall": True,
        "writer_join_timeout": 5.0,
    }
    defaults.update(kwargs)
    provider = RemoteSemanticProvider(**defaults)
    provider._client = client  # type: ignore[assignment]
    provider.initialize(session_id="s1")
    return provider


def test_queue_prefetch_fires_thread() -> None:
    """queue_prefetch 应启动一个 daemon 线程。"""
    client = _FakeClient(slow=0.2)
    provider = _make_provider(client)

    assert provider._prefetch_thread is None
    provider.queue_prefetch("测试查询")
    assert provider._prefetch_thread is not None
    assert provider._prefetch_thread.is_alive()
    provider._prefetch_thread.join(timeout=3.0)
    provider.shutdown()
    print("  queue_prefetch_fires_thread OK")


def test_prefetch_consumes_cached_result() -> None:
    """queue_prefetch 完成后，prefetch 应直接取缓存，不发新 HTTP。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)

    provider.queue_prefetch("用户信息")
    provider._prefetch_thread.join(timeout=3.0)

    call_count_before = len(client.calls)
    result = provider.prefetch("另一个查询")
    call_count_after = len(client.calls)

    assert result != "", "prefetch should return cached result"
    assert "李雷" in result, f"expected '李雷' in result, got: {result}"
    assert call_count_after == call_count_before, (
        f"prefetch should NOT make new HTTP call when cache is warm, "
        f"but made {call_count_after - call_count_before} extra call(s)"
    )
    provider.shutdown()
    print("  prefetch_consumes_cached_result OK")


def test_timeout_fallback_to_sync() -> None:
    """后台线程超过 3s 未完成时，prefetch 应 fallback 到同步调用。

    验证：join 超时后不会无限等待，而是走 sync fallback。
    总耗时 ≈ 3s (join timeout) + slow (sync fallback) ≈ 8s。
    关键不变量：join 本身被 3s 截断，不会等 5s 的后台线程完成。
    """
    client = _FakeClient(slow=5.0)
    provider = _make_provider(client)

    provider.queue_prefetch("慢查询")

    t0 = time.monotonic()
    result = provider.prefetch("fallback 查询")
    elapsed = time.monotonic() - t0

    # join(3s) + sync fallback(5s) ≈ 8s；如果 join 没截断会是 5+5=10s
    assert elapsed < 9.0, f"prefetch took too long ({elapsed:.1f}s), join timeout may not be working"
    assert elapsed >= 3.0, f"prefetch returned too fast ({elapsed:.1f}s), join timeout not applied"
    assert result != "", "prefetch should fallback to sync and return result"
    assert "李雷" in result
    provider.shutdown()
    print(f"  timeout_fallback_to_sync OK (elapsed {elapsed:.1f}s, join capped at 3s + sync fallback)")


def test_shutdown_prevents_queue_prefetch() -> None:
    """shutdown 后 queue_prefetch 应是 no-op。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)
    provider.shutdown()

    provider.queue_prefetch("不应执行")
    assert provider._prefetch_thread is None, "no thread should start after shutdown"
    print("  shutdown_prevents_queue_prefetch OK")


def test_cold_start_sync_fallback() -> None:
    """无预热时 prefetch 应同步调用并返回结果。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client)

    result = provider.prefetch("冷启动查询")
    assert result != "", "cold start should do sync recall"
    assert "李雷" in result
    assert len(client.calls) == 1, f"expected 1 sync call, got {len(client.calls)}"
    provider.shutdown()
    print("  cold_start_sync_fallback OK")


def test_tools_mode_skips() -> None:
    """tools 模式下 queue_prefetch 和 prefetch 均应跳过。"""
    client = _FakeClient(slow=0.0)
    provider = _make_provider(client, memory_mode="tools")

    provider.queue_prefetch("不应执行")
    assert provider._prefetch_thread is None

    result = provider.prefetch("也不应执行")
    assert result == ""
    assert len(client.calls) == 0
    provider.shutdown()
    print("  tools_mode_skips OK")


def main() -> int:
    tests = [
        test_queue_prefetch_fires_thread,
        test_prefetch_consumes_cached_result,
        test_timeout_fallback_to_sync,
        test_shutdown_prevents_queue_prefetch,
        test_cold_start_sync_fallback,
        test_tools_mode_skips,
    ]
    print(f"Running {len(tests)} V13 prefetch tests...")
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
