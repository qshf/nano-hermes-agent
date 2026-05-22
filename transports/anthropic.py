"""V18 — AnthropicTransport（Anthropic Messages API）。

设计与裁剪
----------
源项目 ``anthropic_adapter.py`` 2064 行，包含 OAuth / Claude Code 前缀 /
thinking signature 处理 / Kimi 兼容 / 多种 image source 转换等生产级复杂度。
nano 只保留**核心格式差异**：

1. ``convert_messages`` — 拆出 system（Anthropic 独立参数）+ assistant tool_calls
   转 tool_use blocks + tool result 转 user message with tool_result content
2. ``convert_tools`` — OpenAI ``{type:"function", function:{name,description,parameters}}``
   → Anthropic ``{name, description, input_schema}``
3. ``build_kwargs`` — 组装 ``{model, system, messages, tools, max_tokens}``
   + DashScope 要求 ``thinking={"type":"disabled"}``
4. ``normalize_response`` — 解析 content blocks（text / tool_use）→ NormalizedResponse

真跑验证：Qwen via DashScope Anthropic 端点
    base_url = https://dashscope.aliyuncs.com/apps/anthropic
    model = qwen3.6-plus
    max_tokens 必填、thinking.type=disabled

对应源项目:
    ``hermes-agent/agent/transports/anthropic.py``
    ``hermes-agent/agent/anthropic_adapter.py`` (convert_messages / convert_tools / build_kwargs 子集)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from transports.base import ProviderTransport
from transports.types import NormalizedResponse, ToolCall, Usage


class AnthropicTransport(ProviderTransport):
    """``api_mode="anthropic_messages"`` 的 transport — Anthropic Messages API。

    核心差异（相对 ChatCompletionsTransport）：
    - system 是独立参数，不在 messages 数组里
    - tool 定义用 ``input_schema`` 而非嵌套在 ``function`` 下的 ``parameters``
    - assistant 的 tool 调用是 ``tool_use`` content block，不是顶层 ``tool_calls``
    - tool 结果是 ``user`` 角色消息里的 ``tool_result`` content block
    - 响应是 content blocks 列表（text / tool_use），不是单个 content 字符串
    - stop_reason 词汇不同：``end_turn`` / ``tool_use`` / ``max_tokens``
    """

    _STOP_REASON_MAP = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
    }

    @property
    def api_mode(self) -> str:
        return "anthropic_messages"

    def convert_messages(
        self, messages: List[Dict[str, Any]], **kwargs
    ) -> tuple[Any, List[Dict[str, Any]]]:
        """OpenAI messages → ``(system, anthropic_messages)`` 元组。

        转换规则：
        - role=system → 提取为独立 system 字符串
        - role=assistant + tool_calls → 转为含 text + tool_use blocks 的 assistant 消息
        - role=tool → 转为 user 消息里的 tool_result content block
        - role=user → 保持（content 转为 text block 列表）
        """
        system: str | None = None
        result: List[Dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "system":
                system = content if isinstance(content, str) else str(content)
                continue

            if role == "assistant":
                blocks: List[Dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in m.get("tool_calls", []):
                    if not tc or not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    try:
                        parsed = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except (json.JSONDecodeError, ValueError):
                        parsed = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": parsed,
                    })
                if blocks:
                    result.append({"role": "assistant", "content": blocks})
                continue

            if role == "tool":
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": str(content) if content else "",
                }
                # Anthropic 要求 tool_result 在 user 角色消息里
                # 如果上一条已经是 user（含 tool_result），合并进去
                if result and result[-1].get("role") == "user" and isinstance(result[-1].get("content"), list):
                    result[-1]["content"].append(tool_result_block)
                else:
                    result.append({"role": "user", "content": [tool_result_block]})
                continue

            # role=user — 普通用户消息
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            else:
                result.append({"role": "user", "content": content})

        return system, result

    def convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """OpenAI tool schema → Anthropic tool schema。

        OpenAI: ``{"type":"function","function":{"name":"x","description":"y","parameters":{...}}}``
        Anthropic: ``{"name":"x","description":"y","input_schema":{...}}``
        """
        if not tools:
            return []
        result = []
        for t in tools:
            fn = t.get("function", {})
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] | None = None,
        **params,
    ) -> Dict[str, Any]:
        """组装 ``anthropic.messages.create()`` 的 kwargs。

        params 可选项:
            max_tokens (int): 输出 token 上限（Anthropic 必填，默认 4096）
            temperature (float): 采样温度
            timeout (float): API 超时
            thinking (dict): thinking 配置（DashScope 要求 {"type":"disabled"}）
        """
        system, anthropic_messages = self.convert_messages(messages)

        api_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": params.get("max_tokens", 4096),
        }

        if system:
            api_kwargs["system"] = system

        converted_tools = self.convert_tools(tools) if tools else []
        if converted_tools:
            api_kwargs["tools"] = converted_tools

        if params.get("temperature") is not None:
            api_kwargs["temperature"] = params["temperature"]

        if params.get("timeout") is not None:
            api_kwargs["timeout"] = params["timeout"]

        # DashScope Qwen 的 Anthropic 端点要求显式 thinking 配置
        thinking = params.get("thinking", {"type": "disabled"})
        if thinking:
            api_kwargs["thinking"] = thinking

        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Anthropic Message → NormalizedResponse。

        解析 content blocks：
        - type=text → 拼接为 content
        - type=tool_use → 转为 ToolCall
        - type=thinking → 拼接为 reasoning（V18 暂不处理，留作已知简化）
        """
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            block_type = block.type if hasattr(block, "type") else block.get("type")

            if block_type == "text":
                text = block.text if hasattr(block, "text") else block.get("text", "")
                text_parts.append(text)
            elif block_type == "tool_use":
                tc_id = block.id if hasattr(block, "id") else block.get("id", "")
                tc_name = block.name if hasattr(block, "name") else block.get("name", "")
                tc_input = block.input if hasattr(block, "input") else block.get("input", {})
                tool_calls.append(ToolCall(
                    id=tc_id,
                    name=tc_name,
                    arguments=json.dumps(tc_input) if isinstance(tc_input, dict) else str(tc_input),
                ))

        stop_reason = getattr(response, "stop_reason", None) or "end_turn"
        finish_reason = self._STOP_REASON_MAP.get(stop_reason, "stop")

        # Usage
        usage: Usage | None = None
        resp_usage = getattr(response, "usage", None)
        if resp_usage:
            usage = Usage(
                prompt_tokens=getattr(resp_usage, "input_tokens", 0) or 0,
                completion_tokens=getattr(resp_usage, "output_tokens", 0) or 0,
                total_tokens=(
                    (getattr(resp_usage, "input_tokens", 0) or 0)
                    + (getattr(resp_usage, "output_tokens", 0) or 0)
                ),
                cached_tokens=getattr(resp_usage, "cache_read_input_tokens", 0) or 0,
            )

        return NormalizedResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            reasoning=None,
            usage=usage,
            provider_data=None,
        )

    def validate_response(self, response: Any) -> bool:
        """检查 Anthropic 响应结构。"""
        if response is None:
            return False
        content = getattr(response, "content", None)
        if not isinstance(content, list):
            return False
        if not content:
            return getattr(response, "stop_reason", None) == "end_turn"
        return True

    def map_finish_reason(self, raw_reason: str) -> str:
        return self._STOP_REASON_MAP.get(raw_reason, "stop")


# 模块导入时自动注册
from transports import register_transport  # noqa: E402

register_transport("anthropic_messages", AnthropicTransport)
