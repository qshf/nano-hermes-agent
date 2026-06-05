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
from transports.streaming import (
    EVENT_DONE,
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL_STARTED,
    CancelToken,
    StreamEvent,
    StreamIterator,
)
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
        - role=system → 提取为独立 system；content 为 str 则原样返回，为 list[block]
          则保留（V20 prompt_caching 把 str 升级为 list 以挂 cache_control）
        - role=assistant + tool_calls → 转为含 text + tool_use blocks 的 assistant 消息
        - role=tool → 转为 user 消息里的 tool_result content block；上层
          ``msg["cache_control"]`` 会落到 tool_result 块上（V20）
        - role=user → 保持（content 转为 text block 列表，或保留已升级的 list）

        V20 cache_control 保留规则：
        - 上层 ``msg["cache_control"]`` （tool / 空 content 的标记位置）→ 落到
          输出消息的最后一个 content block
        - content 已是 list[block] 且某 block 带 cache_control → 原样保留
        """
        system: Any = None
        result: List[Dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            top_cache = m.get("cache_control")  # prompt_caching 顶层标记

            if role == "system":
                if isinstance(content, list):
                    # 已升级为 [{"type":"text", ..., "cache_control":...}]
                    system = content
                else:
                    system = content if isinstance(content, str) else str(content)
                continue

            if role == "assistant":
                blocks: List[Dict[str, Any]] = []
                if isinstance(content, list):
                    # 已是 block list（带 cache_control）
                    blocks.extend(content)
                elif content:
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
                    if top_cache and isinstance(blocks[-1], dict):
                        blocks[-1]["cache_control"] = top_cache
                    result.append({"role": "assistant", "content": blocks})
                continue

            if role == "tool":
                # tool result 字符串可能已被 prompt_caching 升级为 list；统一回退到 str
                tr_content: Any
                if isinstance(content, list):
                    tr_content = content
                else:
                    tr_content = str(content) if content else ""
                tool_result_block: Dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": tr_content,
                }
                if top_cache:
                    tool_result_block["cache_control"] = top_cache
                # Anthropic 要求 tool_result 在 user 角色消息里
                # 如果上一条已经是 user（含 tool_result），合并进去
                if result and result[-1].get("role") == "user" and isinstance(result[-1].get("content"), list):
                    result[-1]["content"].append(tool_result_block)
                else:
                    result.append({"role": "user", "content": [tool_result_block]})
                continue

            # role=user
            if isinstance(content, list):
                # 已升级为 block list
                user_content: Any = content
                if top_cache and content and isinstance(content[-1], dict):
                    content[-1]["cache_control"] = top_cache
            elif top_cache:
                # 顶层 cache 但 content 是 str / 空 — 升级为 block list 挂 cache_control
                user_content = [{
                    "type": "text",
                    "text": str(content) if content else "",
                    "cache_control": top_cache,
                }]
            else:
                user_content = content
            result.append({"role": "user", "content": user_content})

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
            input_tok = getattr(resp_usage, "input_tokens", 0) or 0
            output_tok = getattr(resp_usage, "output_tokens", 0) or 0
            cache_read = getattr(resp_usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(resp_usage, "cache_creation_input_tokens", 0) or 0
            usage = Usage(
                prompt_tokens=input_tok + cache_read + cache_write,
                completion_tokens=output_tok,
                total_tokens=input_tok + cache_read + cache_write + output_tok,
                cached_tokens=cache_read,
                cache_creation_tokens=cache_write,
            )

        return NormalizedResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            reasoning=None,
            usage=usage,
            provider_data=None,
        )

    def call(self, client: Any, **kwargs) -> NormalizedResponse:
        api_kwargs = self.build_kwargs(**kwargs)
        response = client.messages.create(**api_kwargs)
        if not self.validate_response(response):
            raise ValueError("Invalid response from messages.create")
        return self.normalize_response(response)

    # ── 真流式 ──────────────────────────────────────────────────────
    def stream_call(
        self,
        client: Any,
        cancel_token: Optional[CancelToken] = None,
        **kwargs,
    ) -> StreamIterator:
        """SSE 流式 — Anthropic SDK ``messages.stream()`` 上下文管理器。

        事件模型（与 ChatCompletions 不同）：
        - ``message_start`` — 不发增量，只携带初始 usage（input_tokens / cache_*）
        - ``content_block_start`` — 一个 block 开始；type=tool_use 时含 ``name``/``id``
        - ``content_block_delta`` — block 内增量；delta.type 决定字段：
            ``text_delta`` → ``delta.text``  (非 tool_use 走 text_delta 事件)
            ``thinking_delta`` → ``delta.thinking`` (走 reasoning_delta 事件)
            ``input_json_delta`` → ``delta.partial_json`` (tool_use 输入参数分片)
        - ``content_block_stop`` — block 结束
        - ``message_delta`` — 单帧带 stop_reason 和最终 usage（output_tokens 等）
        - ``message_stop`` — 流结束

        nano 简化：
        - 不在流式期间累积 tool_use 的 input — 因 ``stream.get_final_message()``
          会返回带完整 input 的原生 Message，复用 ``normalize_response`` 即可重建
        - 不处理 thinking signature（reasoning 增量直接 emit，最终 Message 里也有）
        - 不做 mid-stream retry / 卡帧检测

        中断：每帧 ``cancel_token.check()``，命中即 raise ``StreamCancelled``，
        SDK 的 ``with`` 上下文负责关闭底层 SSE 连接。
        """
        api_kwargs = self.build_kwargs(**kwargs)

        with client.messages.stream(**api_kwargs) as stream:
            for event in stream:
                if cancel_token is not None:
                    cancel_token.check()

                event_type = getattr(event, "type", None)

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block is not None and getattr(block, "type", None) == "tool_use":
                        tool_name = getattr(block, "name", None)
                        if tool_name:
                            yield StreamEvent(
                                type=EVENT_TOOL_CALL_STARTED,
                                tool_name=tool_name,
                            )
                    continue

                if event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    delta_type = getattr(delta, "type", None)
                    if delta_type == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            yield StreamEvent(type=EVENT_TEXT_DELTA, text=text)
                    elif delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", "") or ""
                        if thinking:
                            yield StreamEvent(
                                type=EVENT_REASONING_DELTA, text=thinking,
                            )
                    # input_json_delta 不暴露 — get_final_message 会重建完整 input
                    continue

                # message_start / content_block_stop / message_delta / message_stop
                # 在 nano 都不需要单独发事件 — 累积责任交给 SDK。

            # 流结束 — 取原生 Message 并复用 normalize_response 重建
            final_msg = stream.get_final_message()

        resp = self.normalize_response(final_msg)
        yield StreamEvent(type=EVENT_DONE, response=resp)

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

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """V20 — 抽取 Anthropic prompt cache 命中/写入 token。

        Anthropic 在 ``response.usage`` 上暴露两个字段：
        - ``cache_read_input_tokens``: 命中缓存（按 ~1/10 input 价计费）
        - ``cache_creation_input_tokens``: 首次写入缓存（按 ~1.25x input 价计费）

        两者皆 0 时返回 None — 表示未启用 cache 或本次未命中。
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if cached == 0 and written == 0:
            return None
        return {"cached_tokens": cached, "creation_tokens": written}

    def apply_prompt_cache(
        self,
        messages: List[Dict[str, Any]],
        cache_ttl: str = "5m",
    ) -> List[Dict[str, Any]]:
        """V20 — Anthropic prompt cache 显式标记（``system_and_3`` 策略）。

        在 system + 最后 3 条非 system 消息上打 cache_control，最多 4 个
        breakpoint。返回深拷贝，原 list 不变（避免跨轮污染）。
        """
        from transports.prompt_caching import apply_anthropic_cache_control
        return apply_anthropic_cache_control(messages, cache_ttl=cache_ttl)


# 模块导入时自动注册
from transports import register_transport  # noqa: E402

register_transport("anthropic_messages", AnthropicTransport)
