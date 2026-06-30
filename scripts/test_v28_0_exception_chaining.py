#!/usr/bin/env python3
"""v0.28.0 不变量测试 — 异常链 `raise FailoverExhausted(...) from exc`。

本档纯机械改动：链全挂时把**最后一个原始异常**挂到 ``FailoverExhausted.__cause__``，
保住 traceback 因果链。这组断言守的是「行为契约」而非实现细节：

- 不断言「内部 errors 列表」如何收集（那是实现），只断言**可观察的 __cause__**。
- 即使将来 chain.py 换个写法收集原始异常，只要 __cause__ 仍是真底层异常，测试就该绿；
  若有人把 cause 收集删了（回到只 raise 聚合异常），测试**必须变红**——这正是本档要锁住的。

覆盖两条独立代码路径（同步 call / 流式 stream_call）+ 兜底分支（无原始异常 → from None）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports.chain import (
    FailoverExhausted,
    TransportChain,
    _BreakerState,
    _ChainEntry,
)
from transports.streaming import EVENT_TEXT_DELTA, StreamEvent
from transports.types import NormalizedResponse


# ── Fakes ───────────────────────────────────────────────────────────────────


class _SyncBoom:
    """同步 transport：call() 必抛指定原始异常。"""

    api_mode = "sync_boom"

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def call(self, client, **kw):
        raise self._exc

    def apply_prompt_cache(self, messages, cache_ttl="5m"):
        return messages


class _StreamBoom:
    """流式 transport：首帧前 / 首帧后可控地抛异常。"""

    api_mode = "stream_boom"

    def __init__(self, exc: BaseException, *, deliver_first: bool = False) -> None:
        self._exc = exc
        self._deliver_first = deliver_first

    def apply_prompt_cache(self, messages, cache_ttl="5m"):
        return messages

    def stream_call(self, client, cancel_token=None, **kwargs):
        if self._deliver_first:
            yield StreamEvent(type=EVENT_TEXT_DELTA, text="partial")
        raise self._exc


def _mk_entry(transport, *, breaker: _BreakerState | None = None) -> _ChainEntry:
    return _ChainEntry(
        api_mode=transport.api_mode,
        transport=transport,
        client=None,
        model=None,
        breaker=breaker or _BreakerState(),
    )


def _chain(*entries: _ChainEntry, **kw) -> TransportChain:
    kw.setdefault("max_retries", 0)
    return TransportChain(
        list(entries),
        sleep_fn=lambda _: None,
        clock_fn=lambda: 0.0,
        **kw,
    )


# ── 同步路径 ──────────────────────────────────────────────────────────────


def test_sync_single_entry_chains_original_cause() -> None:
    boom = ConnectionError("upstream refused")
    chain = _chain(_mk_entry(_SyncBoom(boom)))
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        assert e.__cause__ is boom, "同步路径未把原始异常挂到 __cause__"
        assert isinstance(e.__cause__, ConnectionError)


def test_sync_cause_is_last_entry_exception() -> None:
    """多家全挂 → __cause__ 是**最后一家**的原始异常（不是第一家）。"""
    first = TimeoutError("primary timed out")
    last = ConnectionError("backup refused")
    chain = _chain(_mk_entry(_SyncBoom(first)), _mk_entry(_SyncBoom(last)))
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        assert e.__cause__ is last, "应挂最后一家的原始异常"
        # attempts 仍记录两家（聚合信息不丢）
        assert len(e.attempts) == 2


def test_sync_cause_survives_internal_retry() -> None:
    """单家 RETRYABLE 重试用尽后，__cause__ 仍是真实底层异常。"""
    boom = TimeoutError("read timeout")  # timeout → RETRYABLE
    chain = _chain(_mk_entry(_SyncBoom(boom)), max_retries=2)
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        assert isinstance(e.__cause__, TimeoutError)
        assert e.__cause__ is boom


def test_sync_all_breakers_open_cause_is_none() -> None:
    """所有 entry 因断路器打开被 skip、一次都没真调 → 无原始异常 → from None。"""
    boom = ConnectionError("never called")
    # 断路器已打开且在冷却期内（opened_at=0.0 是 clock，但 consecutive>=threshold + 未过冷却）
    open_breaker = _BreakerState(consecutive_failures=9, opened_at=-1.0, last_reason="rate_limit")
    chain = _chain(
        _mk_entry(_SyncBoom(boom), breaker=open_breaker),
        cooldown_seconds=60.0,
    )
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        # 没有原始异常可挂 → 显式 from None，__cause__ 为 None
        assert e.__cause__ is None, "无真实调用时不应凭空挂 cause"
        assert e.attempts == []


# ── 流式路径 ──────────────────────────────────────────────────────────────


def test_stream_single_entry_chains_original_cause() -> None:
    boom = ConnectionError("stream upstream refused")
    chain = _chain(_mk_entry(_StreamBoom(boom)))
    try:
        list(chain.stream_call(model="m", messages=[]))
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        assert e.__cause__ is boom, "流式路径未把原始异常挂到 __cause__"
        assert isinstance(e.__cause__, ConnectionError)


def test_stream_cause_is_last_entry_exception() -> None:
    first = TimeoutError("primary stream timed out")
    last = ConnectionError("backup stream refused")
    chain = _chain(_mk_entry(_StreamBoom(first)), _mk_entry(_StreamBoom(last)))
    try:
        list(chain.stream_call(model="m", messages=[]))
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        assert e.__cause__ is last
        assert len(e.attempts) == 2


def test_stream_delivered_midstream_reraises_original_not_exhausted() -> None:
    """已 yield 过事件后失败 → 直接透传原异常（不切家、不抛 FailoverExhausted）。

    回归保护：本档没改这条「已交付则透传」的控制流，确保 errors 收集没把它带歪。
    """
    boom = ConnectionError("died mid-stream")
    chain = _chain(_mk_entry(_StreamBoom(boom, deliver_first=True)))
    try:
        list(chain.stream_call(model="m", messages=[]))
        assert False, "expected original exception to propagate"
    except FailoverExhausted:
        assert False, "已交付增量后不应聚合成 FailoverExhausted"
    except ConnectionError as e:
        assert e is boom


# ── 既有契约回归（确保挂 cause 没改坏聚合消息） ───────────────────────────


def test_exhausted_message_still_aggregates_all_attempts() -> None:
    chain = _chain(_mk_entry(_SyncBoom(TimeoutError("t1"))), _mk_entry(_SyncBoom(ConnectionError("c2"))))
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        # 聚合消息仍含全部 attempts（cause 是额外信息，不替代它）
        assert "All transports failed" in str(e)
        assert len(e.attempts) == 2


def test_sync_response_type_unchanged_on_success() -> None:
    """烟雾：本档不改成功路径——成功仍返回 NormalizedResponse，无异常。"""

    class _Ok:
        api_mode = "ok"

        def call(self, client, **kw):
            return NormalizedResponse(content="fine", tool_calls=None, finish_reason="stop", usage=None)

        def apply_prompt_cache(self, messages, cache_ttl="5m"):
            return messages

    chain = _chain(_mk_entry(_Ok()))
    resp = chain.call(model="m", messages=[])
    assert isinstance(resp, NormalizedResponse)
    assert resp.content == "fine"


# ── cause 的可见出口（消费点 logger exc_info 展开 __cause__） ─────────────


def test_logged_traceback_includes_cause_chain() -> None:
    """v0.28.0 的 cause 必须有出口：消费点 logger.error(exc_info=True) 时，
    格式化的 traceback 应包含原始异常的因果链文字。

    挂 __cause__ 只是把因果挂到对象上；真正能被人追溯，靠消费点
    (turn_loop / child_loop) 用 ``exc_info=True`` 把它喂给 logging，
    由标准 traceback 展开。这条断言守的就是「cause 进得了日志」这个出口，
    而不是给用户看的那行聚合 print 摘要。
    """
    import logging
    import traceback

    boom = ConnectionError("upstream refused XYZ-marker")
    chain = _chain(_mk_entry(_SyncBoom(boom)))
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        # 模拟消费点 logger.error("...: %s", e, exc_info=True) 实际格式化出的内容
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname="", lineno=0,
            msg="all transports failed: %s", args=(e,),
            exc_info=(type(e), e, e.__traceback__),
        )
        rendered = logging.Formatter().format(record)
        # 标准 traceback 展开 __cause__ 的连接语 + 原始异常的具体信息都应出现
        assert "direct cause" in rendered, "traceback 未展开 __cause__"
        assert "upstream refused XYZ-marker" in rendered, "原始异常信息没进日志"
        assert "ConnectionError" in rendered
        # 不依赖 logging 实现，直接 format_exception 也应拿到 cause
        text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        assert "upstream refused XYZ-marker" in text


# ── 入口 ────────────────────────────────────────────────────────────────────


TESTS = [
    test_sync_single_entry_chains_original_cause,
    test_sync_cause_is_last_entry_exception,
    test_sync_cause_survives_internal_retry,
    test_sync_all_breakers_open_cause_is_none,
    test_stream_single_entry_chains_original_cause,
    test_stream_cause_is_last_entry_exception,
    test_stream_delivered_midstream_reraises_original_not_exhausted,
    test_exhausted_message_still_aggregates_all_attempts,
    test_sync_response_type_unchanged_on_success,
    test_logged_traceback_includes_cause_chain,
]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")

    print()
    if failed:
        print(f"FAILED: {failed}/{len(TESTS)}")
        return 1
    print(f"ALL {len(TESTS)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
