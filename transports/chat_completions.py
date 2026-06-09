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

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

from transports.base import ProviderTransport
from transports.streaming import (
    EVENT_DONE,
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_ARGUMENTS_DELTA,
    EVENT_TOOL_ARGUMENTS_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    CancelToken,
    StreamEvent,
    StreamIterator,
)
from transports.types import NormalizedResponse, ToolCall, Usage


@dataclass
class _ChatStreamAccumulator:
    """OpenAI 兼容 SSE chunks → 与非流式 ``ChatCompletion`` 同形态的 SimpleNamespace。

    源项目 ``run_agent.py:7666-7879`` 的同款做法：流式只管"把分片黏起来"，最终
    构造一个 duck-typed 假对象喂回 ``normalize_response`` — 字段抽取
    （content / tool_calls / usage / reasoning_content）只在一处定义、不重复。

    provider quirk 集中在 ``absorb``：
    - ``function.name`` 用赋值而非 ``+=``：MiniMax M2.7 via NVIDIA NIM 会在每帧
      重发完整 name；``+=`` 会得到 ``"read_fileread_file"``
    - ``function.arguments`` 必须 concat：OpenAI spec 分片下发
    - ``usage`` 在最终 ``choices=[]`` 帧带 — 需 ``stream_options.include_usage``
    """

    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    started_idx: set[int] = field(default_factory=set)
    finish_reason: Optional[str] = None
    usage_obj: Any = None

    def absorb(self, chunk: Any) -> StreamIterator:
        """消费一个 SSE chunk，按需 yield 增量事件，状态写回 self。"""
        if not chunk.choices:
            if getattr(chunk, "usage", None):
                self.usage_obj = chunk.usage
            return

        choice0 = chunk.choices[0]
        delta = choice0.delta

        if delta is not None:
            rtxt = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
            if rtxt:
                self.reasoning.append(rtxt)
                yield StreamEvent(type=EVENT_REASONING_DELTA, text=rtxt)

            if getattr(delta, "content", None):
                self.content.append(delta.content)
                yield StreamEvent(type=EVENT_TEXT_DELTA, text=delta.content)

            for tcd in getattr(delta, "tool_calls", None) or ():
                idx = tcd.index if tcd.index is not None else 0
                entry = self.tool_calls.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""},
                )
                if tcd.id:
                    entry["id"] = tcd.id
                fn = getattr(tcd, "function", None)
                if fn is not None:
                    if fn.name:
                        entry["name"] = fn.name  # 赋值，非 +=
                    if fn.arguments:
                        entry["arguments"] += fn.arguments
                        yield StreamEvent(
                            type=EVENT_TOOL_ARGUMENTS_DELTA,
                            tool_name=entry["name"] or None,
                            tool_call_id=entry["id"] or None,
                            argument_field="arguments",
                            delta_chars=len(fn.arguments),
                            total_chars=len(entry["arguments"]),
                        )
                if entry["name"] and idx not in self.started_idx:
                    self.started_idx.add(idx)
                    yield StreamEvent(
                        type=EVENT_TOOL_CALL_STARTED,
                        tool_name=entry["name"],
                    )

        if choice0.finish_reason:
            self.finish_reason = choice0.finish_reason
            for entry in self.tool_calls.values():
                if entry.get("arguments"):
                    yield StreamEvent(
                        type=EVENT_TOOL_ARGUMENTS_FINISHED,
                        tool_name=entry.get("name") or None,
                        tool_call_id=entry.get("id") or None,
                        argument_field="arguments",
                        total_chars=len(entry.get("arguments") or ""),
                    )

        if getattr(chunk, "usage", None):
            self.usage_obj = chunk.usage

    def to_chat_completion(self) -> SimpleNamespace:
        """构造与 ``ChatCompletion`` 同形态的 mock — 喂回 ``normalize_response``。"""
        tool_calls = None
        if self.tool_calls:
            tool_calls = [
                SimpleNamespace(
                    id=self.tool_calls[i]["id"],
                    function=SimpleNamespace(
                        name=self.tool_calls[i]["name"],
                        arguments=self.tool_calls[i]["arguments"],
                    ),
                )
                for i in sorted(self.tool_calls)
            ]
        msg = SimpleNamespace(
            content="".join(self.content) or None,
            tool_calls=tool_calls,
            reasoning=None,
            reasoning_content="".join(self.reasoning) or None,
        )
        choice = SimpleNamespace(
            message=msg, finish_reason=self.finish_reason or "stop",
        )
        return SimpleNamespace(choices=[choice], usage=self.usage_obj)


class ChatCompletionsTransport(ProviderTransport):
    """``api_mode="chat_completions"`` 的 transport — OpenAI 兼容协议默认路径。"""

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs
    ) -> list[dict[str, Any]]:
        """messages 已是 OpenAI 格式 — identity 直通 + assistant 消息合法性兜底。

        Chat Completions 协议要求每条 ``role: assistant`` 必须满足 ``content``
        非空 *或* 带 ``tool_calls``。否则 400::

            Invalid assistant message: content or tool_calls must be set

        历史消息可能因为旧版回填代码、provider 协议差异、或 deepseek-v4-flash
        把可见正文塞 reasoning_content 等原因，留下"纯 reasoning，无 content，
        无 tool_calls"的脏 assistant 消息。这里在出口处兜一层：
          - 抢救：reasoning_content 非空 → 提升为 content
          - 兜底：占位空格 " "（极罕见，仅为让协议过关）

        写入侧（main.py / child_loop.py）的 build_assistant_history_msg 已经做
        过同样的抢救；这里是"读旧账"的第二道防线。
        """
        sanitized: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") != "assistant":
                sanitized.append(msg)
                continue
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            content_empty = content is None or (isinstance(content, str) and not content.strip())
            if content_empty and not tool_calls:
                # 抢救：reasoning_content 拉上来当 content
                rc = msg.get("reasoning_content")
                fixed = dict(msg)
                if isinstance(rc, str) and rc.strip():
                    fixed["content"] = rc
                else:
                    fixed["content"] = " "  # 极罕见兜底
                sanitized.append(fixed)
            else:
                sanitized.append(msg)
        return sanitized

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

    # ── 真流式 ──────────────────────────────────────────────────────
    def stream_call(
        self,
        client: Any,
        cancel_token: Optional[CancelToken] = None,
        **kwargs,
    ) -> StreamIterator:
        """SSE 流式 — 累积分片 → 重建 ``ChatCompletion`` 同形态 → 复用 ``normalize_response``。

        ``_ChatStreamAccumulator`` 藏住分片消费规则与 provider quirk（name 赋值不
        ``+=`` / args concat / final usage 帧）。此处只串"取流 → 喂分片 → 关流 →
        normalize"四步，与 ``AnthropicTransport.stream_call`` 形态对称。

        中断：每帧 ``cancel_token.check()``，命中即 raise ``StreamCancelled``，
        ``finally`` 兜底 close stream。
        """
        api_kwargs = self.build_kwargs(**kwargs)
        api_kwargs["stream"] = True
        api_kwargs["stream_options"] = {"include_usage": True}

        acc = _ChatStreamAccumulator()
        stream = client.chat.completions.create(**api_kwargs)
        try:
            for chunk in stream:
                if cancel_token is not None:
                    cancel_token.check()
                yield from acc.absorb(chunk)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        yield StreamEvent(
            type=EVENT_DONE,
            response=self.normalize_response(acc.to_chat_completion()),
        )

    def validate_response(self, response: Any) -> bool:
        """检查 ``response.choices`` 非空。"""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        return bool(response.choices)

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """V20 — 抽 OpenAI 兼容协议的 cache 命中 token。

        DeepSeek/OpenAI 把 cache 命中数放在 ``usage.prompt_tokens_details.cached_tokens``。
        与 Anthropic 不同的是 — OpenAI 兼容侧的 cache 是**隐式**的（自动 prefix 匹配，
        调用方不打 cache_control），所以这里只能"读取已命中"，无法主动控制写入。

        creation_tokens 字段在 OpenAI 兼容侧不存在 — DeepSeek 不区分 read/write，
        统一用 ``cached_tokens``。返回 0 时表示未命中或不支持。
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            return None
        cached = getattr(details, "cached_tokens", 0) or 0
        if cached == 0:
            return None
        return {"cached_tokens": cached, "creation_tokens": 0}


# 模块导入时自动注册。
from transports import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)
