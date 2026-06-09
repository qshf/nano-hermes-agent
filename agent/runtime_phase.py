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

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.turn_events import PhasePreview

PHASE_ASSISTANT_GENERATING_TEXT = "assistant_generating_text"
PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS = "assistant_generating_tool_arguments"
PHASE_TOOL_EXECUTING = "tool_executing"
PHASE_CHILD_AGENT_RUNNING = "child_agent_running"

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
