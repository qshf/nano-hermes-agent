"""Shared mutable runtime flags for the agent loop and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from transports.streaming import CancelToken


# V23.4: 父子共享的 session-level token 累计字段名集合 —— 与 transports.types.Usage
# 的语义对齐（input/output/cache_read/cache_write）。提取成常量是为了让
# delegate_tool / main.py 累加时少出 typo。
SESSION_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


def _empty_session_tokens() -> dict[str, int]:
    return {k: 0 for k in SESSION_TOKEN_KEYS}


@dataclass
class AgentRuntime:
    """Small shared state object for settings that can change while running.

    Owned by ``main.run_agent``; passed to ``AgentCtx`` (slash commands) and
    ``DelegateContext`` (delegate_task → child loops) so all three share one
    truth source for ``stream_enabled`` and ``cancel_token``.

    V23.4: 加 ``session_tokens`` —— 父子共享的累计 token 计数器。父 turn 结束
    后累加；子 agent（worker 线程）跑完后由 delegate_tool 在 ``_SESSION_TOKENS_LOCK``
    保护下累加。``/transport`` 命令读取这一份做轻量展示。

    设计权衡（vs 单独的 SessionTokens 类）
    --------------------------------------
    - 当前只是 4 个 int 累加，加一个 dict 字段足够；独立类是过度设计
    - AgentRuntime 已经是父子共享真理源（``cancel_token`` / ``stream_enabled``），
      新累加字段加在这里语义自然延续 —— 不必再做一次"父子共享对象"的注入路线
    - V24 trajectory / insights 真要做 cost breakdown / 历史落盘时再独立类化，
      届时用 ``runtime.session_tokens`` 替代点已经全部对齐到一个调用面
    """

    stream_enabled: bool
    cancel_token: Optional[CancelToken]
    # V23.4: 4 维 token 累计（与 ``transports.types.Usage`` 字段同语义）
    # input          = prompt_tokens 总和
    # output         = completion_tokens 总和
    # cache_read     = Anthropic cache 命中 tokens（OpenAI 兼容 = 0）
    # cache_write    = Anthropic cache_creation tokens（OpenAI 兼容 = 0）
    session_tokens: dict[str, int] = field(default_factory=_empty_session_tokens)
