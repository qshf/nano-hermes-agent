"""
RemoteSemanticProvider — V10 外部 Provider（HTTP 客户端）。

把语义记忆封装成独立 HTTP 服务，provider 通过 httpx 走 REST 调用。
不暴露 tool（召回隐式发生在 prefetch 钩子里），让我们看清"prefetch
是钩子做事而不是 tool"的另一条路径 — 与 builtin provider 形成对照。

钩子到端点的 1:1 映射：
    is_available()       GET  /healthz
    initialize()         (无网络，仅缓存 base_url 和 session_id)
    prefetch(query)      POST /recall   {query, session_id, k}
    sync_turn(u, a)      POST /sync     {user, assistant, session_id}
    system_prompt_block  (无网络，固定一行文本)
    get_tool_schemas     (返回空列表)
    shutdown()           关闭 httpx client

对应源项目：plugins/memory/mem0/__init__.py、plugins/memory/supermemory/__init__.py
（两者都是 HTTP 客户端 provider，发送请求到外部 SaaS 后端）

简化（相比源项目）：
- 同步阻塞调用（无后台线程，无 _ext_prefetch_cache 预热下一轮）
- 无熔断器（circuit breaker）和重试退避
- 失败仅打 warning 返回空 — manager 已做错误隔离，这里不再包一层
- 无认证（mock 服务无需 API key）

保留：完整 MemoryProvider 接口实现、HTTP 边界、钩子 1:1 映射端点。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from memory.provider import MemoryProvider

logger = logging.getLogger(__name__)


class RemoteSemanticProvider(MemoryProvider):
    """通过 HTTP 调用远端语义记忆服务。

    base_url 通过环境变量 MEMORY_SERVICE_URL 传入（详见 agent.py）。
    Mock 服务实现见 scripts/mock_memory_server.py。
    """

    def __init__(
        self,
        base_url: str,
        *,
        top_k: int = 3,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._top_k = top_k
        self._session_id: str = ""
        self._client = httpx.Client(timeout=timeout)

    # -- 标识 ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "remote_semantic"

    # -- 生命周期 --------------------------------------------------------

    def is_available(self) -> bool:
        """探活 — 检查远端 /healthz 端点。

        失败时返回 False，manager 不会注册或会跳过本 provider。
        注意：这里允许网络调用（与 ABC 注释的"不做网络调用"略偏），
        因为 HTTP provider 的本质就是依赖远端服务在线。
        """
        try:
            resp = self._client.get(f"{self._base_url}/healthz")
            return resp.status_code == 200
        except Exception as e:
            logger.warning("RemoteSemantic /healthz failed: %s", e)
            return False

    def initialize(self, session_id: str = "", **kwargs) -> None:
        """缓存 session_id — 后续 recall/sync 都带上它做隔离。"""
        self._session_id = session_id

    def shutdown(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # -- prompt / 工具 ---------------------------------------------------

    def system_prompt_block(self) -> str:
        """固定一行 — 告诉模型有外部长期记忆可用。

        实际召回内容走 prefetch 注入 user message，不在这里展开
        （静态 system prompt 不能含每轮变化的内容，否则破坏前缀缓存）。
        """
        return "Long-term semantic memory is available; relevant context will be recalled per turn."

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """无工具 — 召回是隐式的（prefetch 钩子触发）。

        与 BuiltinMemoryProvider 形成对照：
        - builtin 用 tool 让模型显式调用 add/replace/remove
        - remote_semantic 用 prefetch 钩子在每轮自动召回
        """
        return []

    # -- V9 生命周期钩子 -------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮前调用远端 /recall 端点，返回拼接好的召回文本。

        失败返回空串 — manager 的 prefetch_all 会跳过空结果，
        不会把空围栏注入 user message。
        """
        payload = {
            "query": query,
            "session_id": session_id or self._session_id,
            "k": self._top_k,
        }
        try:
            resp = self._client.post(f"{self._base_url}/recall", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("RemoteSemantic /recall failed: %s", e)
            return ""

        hits = data.get("hits") or []
        if not hits:
            return ""

        lines = []
        for i, hit in enumerate(hits, 1):
            text = (hit.get("text") or "").strip()
            score = hit.get("score")
            if not text:
                continue
            if isinstance(score, (int, float)):
                lines.append(f"[{i}] (score={score:.3f}) {text}")
            else:
                lines.append(f"[{i}] {text}")
        return "\n".join(lines)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """每轮 tool loop 结束后，把对话推到 /sync 端点持久化。

        同步阻塞 — 源项目用后台线程入队避免阻塞下一轮，nano 简化为同步。
        失败仅打 warning，不抛 — agent 主循环不应因记忆持久化失败而中断。
        """
        payload = {
            "user": user_content,
            "assistant": assistant_content,
            "session_id": session_id or self._session_id,
        }
        try:
            resp = self._client.post(f"{self._base_url}/sync", json=payload)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("RemoteSemantic /sync failed: %s", e)
