"""V17 — ChatCompletionsTransport（OpenAI 兼容）。

设计与裁剪
----------
源项目 ``transports/chat_completions.py`` 614 行，**绝大部分是 provider quirks**
（Moonshot tool schema / Gemini thinking / OpenRouter cache / LM Studio reasoning
effort 等 16+ provider 的特化路径）。这些对教学没有新增贡献，nano 全部省略。

本文件只保留**核心模式**：
1. ``convert_messages`` — identity（OpenAI 格式直通）
2. ``convert_tools`` — identity
3. ``build_kwargs`` — 组装最小调用参数
4. ``normalize_response`` — ``ChatCompletion`` → ``NormalizedResponse``

DeepSeek / Qwen OpenAI compat 端点 / 任何走 ``chat.completions`` 的家族都用这条
路径。V18 起的 ``AnthropicTransport`` 才是验证 ABC 价值的地方。

对应源项目: ``hermes-agent/agent/transports/chat_completions.py:102-595``
（裁掉 ``_build_kwargs_from_profile`` 和所有 provider quirks）。
"""

from __future__ import annotations

from typing import Any

from transports.base import ProviderTransport
from transports.types import NormalizedResponse, ToolCall, Usage


class ChatCompletionsTransport(ProviderTransport):
    """``api_mode="chat_completions"`` 的 transport — OpenAI 兼容协议默认路径。"""

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs
    ) -> list[dict[str, Any]]:
        """messages 已是 OpenAI 格式 — identity 直通。"""
        return messages

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """tools 已是 OpenAI function calling 格式 — identity 直通。"""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """组装 ``chat.completions.create()`` 的 kwargs。

        params 可选项:
            timeout (float): API 超时
            max_tokens (int): 最大输出 token
            temperature (float): 采样温度
            extra_body (dict): 透传 SDK 的 extra_body（特殊 provider 用）
        """
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self.convert_messages(messages),
        }

        if tools:
            api_kwargs["tools"] = self.convert_tools(tools)

        for opt_key in ("timeout", "max_tokens", "temperature"):
            v = params.get(opt_key)
            if v is not None:
                api_kwargs[opt_key] = v

        extra_body = params.get("extra_body")
        if extra_body:
            api_kwargs["extra_body"] = extra_body

        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """``ChatCompletion`` → ``NormalizedResponse``。

        chat_completions 形态下几乎是 identity — content / tool_calls /
        finish_reason / usage 字段都已经是标准 OpenAI shape，只需要把对象
        属性搬到数据类里。reasoning_content（DeepSeek/Moonshot 独有）
        塞进 ``provider_data``。
        """
        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls: list[ToolCall] | None = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                )
                for tc in msg.tool_calls
            ]

        usage: Usage | None = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
                cached_tokens=(
                    getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                ),
            )

        # DeepSeek/Moonshot 用 reasoning_content；OpenRouter 等用 reasoning。
        # 两个字段语义略不同（前者是模型隐式 CoT，后者是显式 thinking），分开存。
        reasoning = getattr(msg, "reasoning", None)
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None and hasattr(msg, "model_extra"):
            model_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(model_extra, dict):
                reasoning_content = model_extra.get("reasoning_content")

        provider_data: dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content

        return NormalizedResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def call(self, client: Any, **kwargs) -> NormalizedResponse:
        api_kwargs = self.build_kwargs(**kwargs)
        response = client.chat.completions.create(**api_kwargs)
        if not self.validate_response(response):
            raise ValueError("Invalid response from chat.completions.create")
        return self.normalize_response(response)

    def validate_response(self, response: Any) -> bool:
        """检查 ``response.choices`` 非空。"""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        return bool(response.choices)


# 模块导入时自动注册。
from transports import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)
