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

    # ── 可选 hook ───────────────────────────────────────────────

    def validate_response(self, response: Any) -> bool:
        """检查原生响应结构合法（有 choices / content 等）。默认不校验。"""
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """抽 provider 特定的 cache 命中/写入 token 数。默认 None。"""
        return None

    def map_finish_reason(self, raw_reason: str) -> str:
        """provider stop reason → OpenAI 标准（"stop"/"tool_calls"/"length"）。

        默认透传不映射；Anthropic 会把 ``end_turn`` → ``stop`` 等。
        """
        return raw_reason
