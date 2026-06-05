"""V17 — Provider transport ABC（仿源项目 ``transports/base.py``）。

一个 transport 拥有「和 LLM 对话」的 data path：

  convert_messages → convert_tools → build_kwargs → SDK call → normalize_response

它**不**拥有：client 构造、streaming、credential 刷新、prompt cache、
中断处理、retry — 这些都留在 agent loop 或专门的层。

每个 LLM 家族（OpenAI 兼容 / Anthropic / Bedrock / Codex Responses API）会有
独立的 transport 实现，agent loop 通过 ``api_mode`` 分发到具体 transport。

对应源项目: ``hermes-agent/agent/transports/base.py``。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from transports.streaming import CancelToken, StreamIterator
from transports.types import NormalizedResponse


class ProviderTransport(ABC):
    """provider 特定的格式转换 + 响应标准化抽象基类。

    5 个核心抽象方法 + 3 个可选 hook（默认实现见下方）。
    """

    @property
    @abstractmethod
    def api_mode(self) -> str:
        """该 transport 处理的 api_mode 字符串（如 ``"chat_completions"``）。"""
        ...

    @abstractmethod
    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """OpenAI 格式 messages → provider 原生格式。

        chat_completions 是 identity；Anthropic 会拆出独立的 ``system`` 字段
        并返回 ``(system_str, messages_list)`` 元组。
        """
        ...

    @abstractmethod
    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """OpenAI 格式 tool 定义 → provider 原生格式。

        chat_completions 是 identity；Anthropic 会改写成 ``input_schema`` 形式。
        """
        ...

    @abstractmethod
    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """组装传给 SDK ``client.create(**kwargs)`` 的完整 kwargs。

        通常内部会调 ``convert_messages`` / ``convert_tools``，再叠上模型参数
        （max_tokens / temperature / timeout / extra_body 等）。
        """
        ...

    @abstractmethod
    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """原生 SDK 响应 → 标准 ``NormalizedResponse``。

        这是唯一返回 transport 层类型的方法 — agent loop 之后只读它。
        """
        ...

    # ── 统一调用入口 ─────────────────────────────────────────────

    @abstractmethod
    def call(self, client: Any, **kwargs) -> NormalizedResponse:
        """统一 LLM 调用 — 调用方不需要知道底层是哪个 SDK 方法。

        内部流程：build_kwargs → SDK call → validate → normalize_response。
        kwargs 透传给 build_kwargs（model, messages, tools, temperature 等）。
        """
        ...

    # ── 流式入口（默认假流式 — 子类可重写为真流式） ────────

    def stream_call(
        self,
        client: Any,
        cancel_token: Optional[CancelToken] = None,
        **kwargs,
    ) -> StreamIterator:
        """流式 LLM 调用 — 返回增量事件迭代器。

        默认实现：调 ``call()`` 拿完整响应后假装流式 — 把 content 一次性
        作为单个 ``text_delta`` 事件 + ``done`` 事件 yield 出去。**子类应
        重写本方法为真流式** —— ChatCompletionsTransport / AnthropicTransport
        在 V22 都重写了。

        默认实现的价值在于：自定义 transport 没实现流式时不会让 agent loop
        崩溃；但 token-by-token 体感缺失，需要看子类。

        参数:
            client: SDK 客户端（chat_completions 用 OpenAI client；anthropic 用
                Anthropic client）
            cancel_token: 取消标记 — 流式循环每帧检查，命中即抛
                ``StreamCancelled``。None 表示不可中断（仅默认假流式路径用）。
            **kwargs: 透传给 ``build_kwargs``（model, messages, tools, ...）

        Yield:
            ``StreamEvent`` 序列；最后一个事件保证是 ``type=="done"``，
            ``response`` 字段含完整 ``NormalizedResponse``。
        """
        from transports.streaming import (
            EVENT_DONE, EVENT_TEXT_DELTA, StreamEvent,
        )

        # 默认实现 = 同步 call + 假装一帧到底
        if cancel_token is not None:
            cancel_token.check()
        resp = self.call(client, **kwargs)
        if resp.content:
            yield StreamEvent(type=EVENT_TEXT_DELTA, text=resp.content)
        yield StreamEvent(type=EVENT_DONE, response=resp)

    # ── 可选 hook ───────────────────────────────────────────────

    def validate_response(self, response: Any) -> bool:
        """检查原生响应结构合法（有 choices / content 等）。默认不校验。"""
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """抽 provider 特定的 cache 命中/写入 token 数。默认 None。"""
        return None

    def apply_prompt_cache(
        self,
        messages: List[Dict[str, Any]],
        cache_ttl: str = "5m",
    ) -> List[Dict[str, Any]]:
        """V20 — 在 messages 上注入 prompt cache 标记。

        默认 identity（chat_completions 不需要主动标记 — DeepSeek/OpenAI 隐式
        缓存 prefix）。AnthropicTransport 重写此方法走 ``apply_anthropic_cache_control``
        显式打 cache_control。

        cache_ttl: ``"5m"`` 或 ``"1h"`` — 缓存生命周期。
        """
        return messages

    def map_finish_reason(self, raw_reason: str) -> str:
        """provider stop reason → OpenAI 标准（"stop"/"tool_calls"/"length"）。

        默认透传不映射；Anthropic 会把 ``end_turn`` → ``stop`` 等。
        """
        return raw_reason
