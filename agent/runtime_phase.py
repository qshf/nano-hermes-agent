"""V27.1 — runtime phase spans for external observers.

A phase span says "the host is alive in this runtime phase". It carries safe
metadata only; it does not decide if anything should be spoken.

命名说明（v27.1 后期重命名）
==========================
- ``PhaseSpan``：一段「开了又关」的运行区间句柄（旧名 ``RuntimePhaseLease``）。
  借用分布式追踪的 span 概念——start→(activity)*→finish/error/cancel。
- ``PhaseTracker``：span 的统一持有者 + 单一 listener 注入点（旧名 ``RuntimePhaseManager``）。
- ``PhaseListener``：「phase 事件往哪流」的抽象回调（旧名 ``PhaseSink``）。
  注意与 ``VoiceEventSink`` 区分——listener 是口子，VoiceEventSink 是真正的 HTTP 出口。
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.turn_events import PhasePreview

PHASE_ASSISTANT_GENERATING_TEXT = "assistant_generating_text"
PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS = "assistant_generating_tool_arguments"
PHASE_TOOL_EXECUTING = "tool_executing"
PHASE_CHILD_AGENT_RUNNING = "child_agent_running"

# 子 agent 跑在 ThreadPoolExecutor worker / 同步 child loop 里，复用父进程的
# 单例 registry（其 _runtime 指向父）。若不抑制，子内部每次 registry.dispatch
# 都会在父 tracker 上开一个 PHASE_TOOL_EXECUTING span —— orchestrator 把子工具
# 当成父工具播报（v27.1 review #1）。用 contextvar 在子执行区间打标：worker
# 线程会继承设置时的 context 副本，互不串扰；父侧只播 PHASE_CHILD_AGENT_RUNNING
# 这一个聚合 span。
_in_child_agent: contextvars.ContextVar[bool] = contextvars.ContextVar("nano_in_child_agent", default=False)


def enter_child_agent_scope() -> contextvars.Token:
    """标记"进入子 agent 执行区间"，返回用于复位的 token。"""

    return _in_child_agent.set(True)


def exit_child_agent_scope(token: contextvars.Token) -> None:
    """复位子 agent 区间标记（与 enter_child_agent_scope 配对）。"""

    _in_child_agent.reset(token)


def in_child_agent_scope() -> bool:
    """当前是否处于子 agent 执行区间（registry phase helper 据此抑制 span）。"""

    return _in_child_agent.get()


_PHASE_COUNTER = itertools.count(1)

PhaseEventType = Literal["phase_started", "phase_activity", "phase_finished", "phase_error", "phase_cancelled"]
PhaseCloseStatus = Literal["finished", "error", "cancelled"]
PhaseListener = Callable[[PhaseEventType, PhasePreview], None]


@dataclass
class PhaseSpan:
    name: str
    listener: PhaseListener
    span_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    activity: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.span_id:
            self.span_id = f"span_{next(_PHASE_COUNTER)}"
        self._emit("phase_started")

    def activity_event(self, **activity: Any) -> None:
        if self.closed:
            return
        self.activity.update(activity)
        self._emit("phase_activity")

    def finish(self, **activity: Any) -> None:
        if self.closed:
            return
        self.activity.update(activity)
        self.closed = True
        self._emit("phase_finished")

    def error(self, **activity: Any) -> None:
        if self.closed:
            return
        self.activity.update(activity)
        self.closed = True
        self._emit("phase_error")

    def cancel(self, **activity: Any) -> None:
        if self.closed:
            return
        self.activity.update(activity)
        self.closed = True
        self._emit("phase_cancelled")

    def preview(self, status: PhaseEventType) -> PhasePreview:
        return PhasePreview(
            name=self.name,
            status=status,
            span_id=self.span_id,
            elapsed_ms=int((time.monotonic() - self.started_at) * 1000),
            activity=dict(self.activity),
        )

    def _emit(self, status: PhaseEventType) -> None:
        try:
            self.listener(status, self.preview(status))
        except Exception:  # noqa: BLE001 - observers never affect runtime work
            return


class PhaseTracker:
    """Small owner for active spans in one AgentRuntime."""

    def __init__(self, listener: PhaseListener | None = None) -> None:
        self._listener = listener or (lambda _status, _phase: None)
        self.active: dict[str, PhaseSpan] = {}

    def set_listener(self, listener: PhaseListener | None) -> None:
        self._listener = listener or (lambda _status, _phase: None)

    def start(self, name: str, *, span_id: str = "", **activity: Any) -> PhaseSpan:
        span = PhaseSpan(name=name, span_id=span_id, listener=self._listener, activity=dict(activity))
        self.active[span.span_id] = span
        return span

    def close(self, span: PhaseSpan, status: PhaseCloseStatus = "finished", **activity: Any) -> None:
        if status == "error":
            span.error(**activity)
        elif status == "cancelled":
            span.cancel(**activity)
        else:
            span.finish(**activity)
        self.active.pop(span.span_id, None)


# ─── tool 执行 span 的统一上下文管理器（v27.1 review #8/#9 altitude）──────────
#
# 之前 registry.dispatch 与 turn_loop 的 memory 工具路径各自手写一遍
# "resolve tracker → 子区间抑制 → start → 在每个返回/异常分支 close"，
# 三处样板 + 各自的 try/except 守护 + 各自重算 result_kind。本 CM 把生命周期
# 收口成一个 ``with``：进入时按需开 span（tracker 缺失 / 子 agent 区间 → 给个
# 空对象，调用点无需判 None），退出时**保证** close —— 正常退出按 finished，
# body 抛异常按 error。body 可中途调 ``span.finish(result_kind=...)`` 附带分类
# 元数据；PhaseSpan 的 ``closed`` 幂等保证 CM 退出时不会二次 emit。


class _NullSpan:
    """tracker 缺失 / 子 agent 区间时的占位 span —— 所有方法 no-op。

    让 ``with tool_span(...) as span:`` 的调用点永远拿到一个可调用对象，
    不必在每次 ``span.activity_event`` / ``span.finish`` 前判 ``if span is not None``。
    """

    closed = True

    def activity_event(self, **activity: Any) -> None:
        return

    def finish(self, **activity: Any) -> None:
        return

    def error(self, **activity: Any) -> None:
        return

    def cancel(self, **activity: Any) -> None:
        return


_NULL_SPAN = _NullSpan()


@contextlib.contextmanager
def tool_span(
    runtime: Any,
    name: str,
    *,
    suppress_in_child: bool = True,
    **start_activity: Any,
) -> Iterator[Any]:
    """围绕一次工具执行开/关一个 phase span，异常安全且永不影响工具本身。

    - ``runtime`` 为 None / 无 ``phase_tracker`` / 处于子 agent 区间（且
      ``suppress_in_child``）→ yield ``_NULL_SPAN``，零开销。
    - body 正常退出 → span.finish()（若 body 已 finish/error，则幂等跳过）。
    - body 抛异常 → span.error(error_type=...) 后**重新抛出**（不吞工具异常）。
    - listener 自身的异常已由 PhaseSpan._emit 吞掉，这里只兜底 start 失败。
    """
    tracker = getattr(runtime, "phase_tracker", None)
    if tracker is None or (suppress_in_child and in_child_agent_scope()):
        yield _NULL_SPAN
        return
    try:
        span = tracker.start(name, **start_activity)
    except Exception:  # noqa: BLE001 — observers must never affect tools
        yield _NULL_SPAN
        return
    try:
        yield span
    except Exception as exc:
        tracker.close(span, status="error", error_type=type(exc).__name__)
        raise
    else:
        tracker.close(span)
