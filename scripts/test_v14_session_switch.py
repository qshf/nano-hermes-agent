"""V14 会话切换行为验证脚本。

验证七个关键不变量：
1. on_session_switch 更新 _session_id
2. 切换前 drain writer queue（旧 session 的 retain 全部完成）
3. 切换时清空 prefetch 缓存
4. 切换后 sync_turn 使用新 session_id
5. in-flight prefetch 被 join + 清空
6. 连续多次切换正确跟踪
7. BuiltinMemoryProvider 的 on_session_switch 是安全 no-op
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.remote_semantic import RemoteSemanticProvider
from memory.builtin import BuiltinMemoryProvider


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "results": [
                {"text": "用户叫李雷", "score": 0.95, "source": "entity"},
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
        "auto_retain": True,
        "auto_recall": True,
        "writer_join_timeout": 5.0,
    }
    defaults.update(kwargs)
    provider = RemoteSemanticProvider(**defaults)
    provider._client = client  # type: ignore[assignment]
    provider.initialize(session_id="old-session")
    return provider


def test_session_switch_updates_session_id() -> None:
    """on_session_switch 应更新 _session_id。"""
    client = _FakeClient()
    provider = _make_provider(client)

    assert provider._session_id == "old-session"
    provider.on_session_switch("new-session", reset=True)
    assert provider._session_id == "new-session"
    provider.shutdown()
    print("  session_switch_updates_session_id OK")


def test_session_switch_drains_writer_queue() -> None:
    """切换前应 drain writer queue — 旧 session 的 retain 全部完成。"""
    client = _FakeClient(slow=0.3)
    provider = _make_provider(client)

    provider.sync_turn("user msg 1", "assistant msg 1")
    provider.sync_turn("user msg 2", "assistant msg 2")

    provider.on_session_switch("new-session", reset=True)

    with client._lock:
        retain_calls = [c for c in client.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 2, f"expected 2 retain calls drained, got {len(retain_calls)}"
    provider.shutdown()
    print("  session_switch_drains_writer_queue OK")


def test_session_switch_clears_prefetch_cache() -> None:
    """切换时应清空 prefetch 缓存。"""
    client = _FakeClient()
    provider = _make_provider(client)

    provider.queue_prefetch("预热查询")
    provider._prefetch_thread.join(timeout=3.0)

    with provider._prefetch_lock:
        assert provider._prefetch_result != "", "prefetch cache should be warm"

    provider.on_session_switch("new-session", reset=False)

    with provider._prefetch_lock:
        assert provider._prefetch_result == "", "prefetch cache should be cleared after switch"
    provider.shutdown()
    print("  session_switch_clears_prefetch_cache OK")


def test_session_switch_sync_uses_new_session() -> None:
    """切换后 sync_turn 应使用新 session_id。"""
    client = _FakeClient()
    provider = _make_provider(client)

    provider.on_session_switch("new-session", reset=True)
    provider.sync_turn("hello", "hi there")

    provider._retain_queue.join()

    with client._lock:
        retain_calls = [c for c in client.calls if "/retain" in c["url"]]
    assert len(retain_calls) == 1
    payload = retain_calls[0]["json"]
    assert payload["document_id"] == "new-session", (
        f"expected document_id='new-session', got '{payload['document_id']}'"
    )
    provider.shutdown()
    print("  session_switch_sync_uses_new_session OK")


def test_inflight_prefetch_joined_and_cleared() -> None:
    """in-flight prefetch 线程应被 join + 缓存清空。"""
    client = _FakeClient(slow=1.0)
    provider = _make_provider(client)

    provider.queue_prefetch("慢查询")
    assert provider._prefetch_thread is not None
    assert provider._prefetch_thread.is_alive()

    t0 = time.monotonic()
    provider.on_session_switch("new-session", reset=False)
    elapsed = time.monotonic() - t0

    assert elapsed >= 1.0, f"should have waited for prefetch thread, elapsed={elapsed:.2f}s"
    with provider._prefetch_lock:
        assert provider._prefetch_result == ""
    assert provider._prefetch_thread is None
    provider.shutdown()
    print(f"  inflight_prefetch_joined_and_cleared OK (waited {elapsed:.1f}s)")


def test_multiple_switches_track_correctly() -> None:
    """连续多次切换应正确跟踪 session_id。"""
    client = _FakeClient()
    provider = _make_provider(client)

    sessions = ["session-a", "session-b", "session-c"]
    for sid in sessions:
        provider.on_session_switch(sid, reset=True)
        assert provider._session_id == sid

    provider.shutdown()
    print("  multiple_switches_track_correctly OK")


def test_builtin_provider_noop() -> None:
    """BuiltinMemoryProvider 的 on_session_switch 应是安全 no-op。"""
    provider = BuiltinMemoryProvider()
    provider.initialize(session_id="s1")
    provider.on_session_switch("s2", reset=True)
    provider.shutdown()
    print("  builtin_provider_noop OK")


def main() -> int:
    tests = [
        test_session_switch_updates_session_id,
        test_session_switch_drains_writer_queue,
        test_session_switch_clears_prefetch_cache,
        test_session_switch_sync_uses_new_session,
        test_inflight_prefetch_joined_and_cleared,
        test_multiple_switches_track_correctly,
        test_builtin_provider_noop,
    ]
    print(f"Running {len(tests)} V14 session switch tests...")
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
