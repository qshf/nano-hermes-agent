"""Shared mutable runtime flags for the agent loop and tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from transports.streaming import CancelToken


@dataclass
class AgentRuntime:
    """Small shared state object for settings that can change while running.

    Owned by ``main.run_agent``; passed to ``AgentCtx`` (slash commands) and
    ``DelegateContext`` (delegate_task → child loops) so all three share one
    truth source for ``stream_enabled`` and ``cancel_token``.
    """

    stream_enabled: bool
    cancel_token: Optional[CancelToken]
