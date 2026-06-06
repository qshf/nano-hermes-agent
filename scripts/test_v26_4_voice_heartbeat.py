"""V26.4 代码级语音心跳（VoiceHeartbeat）不变量验证脚本。

不依赖真实 Runtime / 真包：注入 fake VoiceClient + 受控 busy Event + 直接驱动
``_should_beat`` / ``_run`` 的私有路径，验证触发矩阵与降级语义。

覆盖（10 项）：
门控 / 构造（4）：
 1. threshold<=0 → create_if_available 返回 None（关闭）
 2. client=None 且无法 import → 返回 None（软依赖降级）
 3. health() 抛异常 → 返回 None（Runtime 没起）
 4. health() 通 → 返回实例
触发判定 _should_beat（3）：
 5. busy + 超阈值 → True
 6. 非 busy → False（即使超阈值）
 7. busy 但未超阈值 → False
线程 / 行为（3）：
 8. 后台线程在 busy+超时下真推一条 progress（fake client 收到）
 9. mark_active 刷新基准 → 重置沉默计时，不立刻补播
10. speak 抛异常不冒泡（Runtime 中途挂）+ stop() 后线程退出
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.voice_heartbeat import VoiceHeartbeat


def _busy_event(is_busy: bool = False) -> threading.Event:
    event = threading.Event()
    if is_busy:
        event.set()
    return event


class _FakeClient:
    """duck-typed VoiceClient：记录 speak_intent 调用；可配 health/ speak 抛错。"""

    def __init__(self, *, health_ok: bool = True, speak_raises: bool = False) -> None:
        self._health_ok = health_ok
        self._speak_raises = speak_raises
        self.calls: list[tuple[str, str]] = []

    def health(self) -> dict:
        if not self._health_ok:
            raise RuntimeError("runtime down")
        return {"ok": True}

    def speak_intent(self, intent: str, text: str, **_kw) -> dict:
        if self._speak_raises:
            raise RuntimeError("send failed")
        self.calls.append((intent, text))
        return {"queued": True}


# ─── 门控 / 构造 ──────────────────────────────────────────────────────────


def test_1_threshold_zero_disables():
    hb = VoiceHeartbeat.create_if_available(
        _busy_event(), client=_FakeClient(), threshold_seconds=0
    )
    assert hb is None


def test_2_no_client_degrades():
    # client=None 且 _make_default_client 走真 import；若装了 voice kit 会拿到真
    # client，故这里显式传一个"import 失败"的等价：用 probe 关 + monkeypatch。
    import agent.voice_heartbeat as vh

    orig = vh._make_default_client
    vh._make_default_client = lambda: None
    try:
        hb = VoiceHeartbeat.create_if_available(_busy_event(), threshold_seconds=25)
        assert hb is None
    finally:
        vh._make_default_client = orig


def test_3_health_fail_degrades():
    hb = VoiceHeartbeat.create_if_available(
        _busy_event(),
        client=_FakeClient(health_ok=False),
        threshold_seconds=25,
    )
    assert hb is None


def test_4_health_ok_returns_instance():
    hb = VoiceHeartbeat.create_if_available(
        _busy_event(), client=_FakeClient(health_ok=True), threshold_seconds=25
    )
    assert isinstance(hb, VoiceHeartbeat)


# ─── 触发判定 _should_beat ────────────────────────────────────────────────


def test_5_should_beat_when_busy_and_elapsed():
    busy = _busy_event(is_busy=True)
    hb = VoiceHeartbeat(busy, client=_FakeClient(), threshold_seconds=10)
    hb._last_beat = time.monotonic() - 20  # 已沉默 20s > 阈值 10s
    assert hb._should_beat(time.monotonic()) is True


def test_6_no_beat_when_not_busy():
    busy = _busy_event()
    hb = VoiceHeartbeat(busy, client=_FakeClient(), threshold_seconds=10)
    hb._last_beat = time.monotonic() - 100  # 超时很久，但非 busy
    assert hb._should_beat(time.monotonic()) is False


def test_7_no_beat_when_not_elapsed():
    busy = _busy_event(is_busy=True)
    hb = VoiceHeartbeat(busy, client=_FakeClient(), threshold_seconds=10)
    hb._last_beat = time.monotonic() - 2  # 才沉默 2s < 阈值 10s
    assert hb._should_beat(time.monotonic()) is False


# ─── 线程 / 行为 ──────────────────────────────────────────────────────────


def test_8_thread_emits_progress_when_busy_elapsed():
    busy = _busy_event(is_busy=True)
    client = _FakeClient()
    # 阈值设极小（0.05s），_TICK 默认 1s 太慢 —— 直接 patch tick 到 0.02s。
    import agent.voice_heartbeat as vh

    orig_tick = vh._TICK_SECONDS
    vh._TICK_SECONDS = 0.02
    try:
        hb = VoiceHeartbeat(busy, client=client, threshold_seconds=0.05)
        hb.start()
        time.sleep(0.3)  # 足够跑若干 tick 触发至少一次
        hb.stop()
    finally:
        vh._TICK_SECONDS = orig_tick
    assert len(client.calls) >= 1, client.calls
    intent, text = client.calls[0]
    assert intent == "progress", intent
    assert text  # 非空存活文案


def test_9_mark_active_resets_timer():
    busy = _busy_event(is_busy=True)
    hb = VoiceHeartbeat(busy, client=_FakeClient(), threshold_seconds=10)
    hb._last_beat = time.monotonic() - 100  # 本应触发
    hb.mark_active()  # 刷新基准
    assert hb._should_beat(time.monotonic()) is False  # 重置后不再触发


def test_10_speak_error_no_raise_and_stop_exits():
    busy = _busy_event(is_busy=True)
    client = _FakeClient(speak_raises=True)
    import agent.voice_heartbeat as vh

    orig_tick = vh._TICK_SECONDS
    vh._TICK_SECONDS = 0.02
    try:
        hb = VoiceHeartbeat(busy, client=client, threshold_seconds=0.05)
        hb.start()
        time.sleep(0.2)  # 触发 speak → 抛错 → 应被吞，不崩线程
        assert hb._thread is not None and hb._thread.is_alive()  # 线程仍活
        hb.stop()
        assert hb._thread is None  # stop 后清理
    finally:
        vh._TICK_SECONDS = orig_tick


def main() -> None:
    tests = [
        test_1_threshold_zero_disables,
        test_2_no_client_degrades,
        test_3_health_fail_degrades,
        test_4_health_ok_returns_instance,
        test_5_should_beat_when_busy_and_elapsed,
        test_6_no_beat_when_not_busy,
        test_7_no_beat_when_not_elapsed,
        test_8_thread_emits_progress_when_busy_elapsed,
        test_9_mark_active_resets_timer,
        test_10_speak_error_no_raise_and_stop_exits,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except AssertionError as exc:
            print(f"✗ {t.__name__} — {exc}")
            failed += 1
        except Exception as exc:
            import traceback
            print(f"✗ {t.__name__} — UNEXPECTED {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failed += 1

    print("=" * 60)
    if failed:
        print(f"  FAIL: {failed}/{len(tests)} 不变量未通过")
        sys.exit(1)
    else:
        print(f"  PASS: {len(tests)}/{len(tests)} 不变量")


if __name__ == "__main__":
    main()
