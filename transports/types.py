"""V17 — Normalized response types shared across all provider transports.

设计要点
--------
1. **`NormalizedResponse` 是 transport 层唯一暴露给 agent loop 的返回类型。**
   agent loop 不再消费 SDK 原生类型（OpenAI 的 ``ChatCompletion`` / Anthropic 的
   ``Message``），统一读这一份数据类。
2. **向后兼容 properties** — agent.py 现有的 ``tc.function.name`` /
   ``tc.function.arguments`` 调用点不必改。``ToolCall`` 暴露 ``function`` /
   ``type`` property 让旧代码原样工作。
3. **`provider_data` 是协议特定的逃生口** — 跨家族通用字段都升到 top-level
   （content / tool_calls / finish_reason / usage / reasoning），少数家族独有的
   字段（如 Anthropic 的 ``reasoning_details``、Codex 的 ``response_item_id``）
   塞进 ``provider_data`` 让协议感知代码自取，不污染共享接口。

对应源项目: ``hermes-agent/agent/transports/types.py``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """跨 provider 标准化的工具调用。

    ``id`` 是协议层的 canonical 标识符 — 后续构造 tool_result 消息时用作
    ``tool_call_id`` (OpenAI) 或 ``tool_use_id`` (Anthropic)。

    ``provider_data`` 是协议特定字段的 dict 容器。例：

    * Anthropic: ``{"input": {...原始 input dict}}``
    * Codex: ``{"call_id": "call_XXX", "response_item_id": "fc_XXX"}``
    * Gemini: ``{"extra_content": {"google": {"thought_signature": "..."}}}``
    """

    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    # ── 向后兼容 ────────────────────────────────────────────────
    # agent.py 现有读法：tc.function.name / tc.function.arguments / tc.type
    # 通过让 .function 返回 self，旧调用点零改动可继续工作。
    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> "ToolCall":
        return self


@dataclass
class Usage:
    """API 返回的 token 用量统计。

    ``cached_tokens`` / ``cache_creation_tokens`` 区分 read/write —
    Anthropic 显式区分，OpenAI 兼容只有 read（write=0）。这是为了让 V20
    chain 能准确区分"省了多少钱"和"花了多少钱写入缓存"。
    """

    prompt_tokens: int = 0 #  输入规模
    completion_tokens: int = 0 # 输出规模
    total_tokens: int = 0 # 总规模（prompt + completion）
    cached_tokens: int = 0 # 从缓存读取的 token 数（Anthropic cache_read_tokens）
    cache_creation_tokens: int = 0  # Anthropic cache_creation_input_tokens


@dataclass
class NormalizedResponse:
    """跨 provider 标准化的 API 响应。

    共享字段是真正跨家族通用的 — 任何调用方都能直接读，不必按 api_mode 分支。
    协议特定状态（reasoning_details / codex 元信息等）放进 ``provider_data``。
    """

    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop" / "tool_calls" / "length" / "content_filter"
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def reasoning_content(self) -> str | None:
        """DeepSeek/Moonshot 的 ``reasoning_content`` 字段（与 ``reasoning`` 区分）。"""
        return (self.provider_data or {}).get("reasoning_content")


def build_tool_call(
    id: str | None,
    name: str,
    arguments: Any,
    **provider_fields: Any,
) -> ToolCall:
    """构造 ``ToolCall`` 的便捷工厂 — arguments 是 dict 时自动 ``json.dumps``。

    额外的 keyword 参数会被打包进 ``provider_data``。
    """
    args_str = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    pd = dict(provider_fields) if provider_fields else None
    return ToolCall(id=id, name=name, arguments=args_str, provider_data=pd)


def build_assistant_history_msg(normalized: NormalizedResponse) -> dict[str, Any]:
    """把 NormalizedResponse 落成下一轮可回传的 OpenAI 历史 assistant 消息。

    Chat Completions 协议合法 assistant 消息必须满足：``content`` 非空 *或*
    带 ``tool_calls``。否则服务端 400::

        Invalid assistant message: content or tool_calls must be set

    某些 provider（DeepSeek thinking / V4 Pro 偶发）会返回 content=None 且无
    tool_calls 但有 reasoning_content —— 直接回填会让下一轮请求被拒。

    抢救策略（按优先级）：
      1. tool_calls 非空 → content=None 合法（OpenAI 正式允许），保留即可
      2. content 非空 → 直接保留
      3. content 为空但有 reasoning_content → 把 reasoning 提升为 content（避免活跃任务消失）
      4. 全空 → 占位空格 " "（极罕见，让协议过关；下游 sanitize 会再处理）

    同时延续 DeepSeek thinking 模式的 reasoning_content padding 约定（带
    tool_calls 但无 reasoning 时 padding=" "），对应 hermes-agent
    run_agent.py:9621-9635。
    """
    content: Any = normalized.content
    tool_calls = list(normalized.tool_calls or [])
    rc = normalized.reasoning_content

    # 第 3/4 步抢救：content 实际为空且无 tool_calls 时把 reasoning 提进 content
    content_is_empty = content is None or (isinstance(content, str) and not content.strip())
    if content_is_empty and not tool_calls:
        if rc and rc.strip():
            content = rc
        else:
            content = " "  # 极罕见兜底；不丢消息，让协议过

    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in tool_calls
        ]

    # DeepSeek thinking 模式：每条 assistant 必须回传 reasoning_content
    if rc is not None:
        msg["reasoning_content"] = rc
    elif tool_calls:
        msg["reasoning_content"] = " "

    return msg
