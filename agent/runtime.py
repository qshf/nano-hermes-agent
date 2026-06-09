"""Shared mutable runtime flags for the agent loop and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent.runtime_phase import PhaseTracker
from agent.voice_orchestrator_client import VoiceEventSink
from transports.streaming import CancelToken


# 父子共享的 session-level token 累计字段名 —— 与 ``transports.types.Usage``
# 字段 1:1 对齐。常量化让 ``delegate_tool`` / ``main.py`` 累加时不会 typo 漏键。
SESSION_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


def _empty_session_tokens() -> dict[str, int]:
    return {k: 0 for k in SESSION_TOKEN_KEYS}


@dataclass
class AgentRuntime:
    """Small shared state object for settings that can change while running.

    Owned by ``main.run_agent``; passed to ``AgentCtx`` (slash commands) and
    ``DelegateContext`` (delegate_task → child loops) so all three share one
    truth source for ``stream_enabled`` / ``cancel_token`` / ``session_tokens``.

    ``session_tokens`` 累加来自两个路径：父 turn 结束后由 ``main.py`` 累加；
    子 agent（worker 线程）跑完后由 ``delegate_tool`` 在 ``_SESSION_TOKENS_LOCK``
    保护下累加。``/transport`` 命令读取这一份做轻量展示。
    """

    stream_enabled: bool
    cancel_token: Optional[CancelToken]
    # 4 维 token 累计 —— 字段名映射到 ``transports.types.Usage``：
    # input        = prompt_tokens 总和
    # output       = completion_tokens 总和
    # cache_read   = Anthropic cache 命中 tokens（OpenAI 兼容路径 = 0）
    # cache_write  = Anthropic cache_creation tokens（OpenAI 兼容路径 = 0）
    session_tokens: dict[str, int] = field(default_factory=_empty_session_tokens)
    # V27.1: external voice orchestrator side-channel. The host submits bounded
    # facts only; the external service owns speech policy and dispatch.
    voice_event_sink: Optional[VoiceEventSink] = None
    phase_tracker: PhaseTracker = field(default_factory=PhaseTracker)
