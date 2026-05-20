"""
RemoteSemanticProvider — V10 引入，V10.1 同步演进出 Hindsight 路线配置。

V10.1 变化（相对 V10）：
- 仅"配置项"层面演进，**HTTP 请求时序 0 改动**——这正是 V7 抽 ABC 的最终验证点。
- 新增 budget（low/mid/high）/ min_score / auto_retain，对齐 Hindsight 的
  recall_budget / recall_min_score / auto_retain 三个核心配置。
- 服务端把假向量换成 OpenAI embedding、把 dict 换成 pgvector，
  Provider 端**一行业务逻辑不改**，只是请求体多带了几个可选字段。

钩子到端点的 1:1 映射（保持不变）：
    is_available()       GET  /healthz
    initialize()         (无网络，仅缓存 base_url 和 session_id)
    prefetch(query)      POST /recall   {query, session_id, k, budget, min_score}
    sync_turn(u, a)      POST /sync     {user, assistant, session_id}
    system_prompt_block  (无网络，固定一行文本)
    get_tool_schemas     (返回空列表)
    shutdown()           关闭 httpx client

对应源项目：plugins/memory/hindsight/__init__.py 的 `local_external` 模式
（指向已有 Hindsight 实例的 HTTP 调用形态）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from memory.provider import MemoryProvider

logger = logging.getLogger(__name__)


# Hindsight 路线：budget 是召回广度的语义档位。
# Provider 端不解析（服务端把 budget → k 的映射兜底），但保留这个抽象层，
# 让运维同学按"质量"而不是"数量"思考召回 — 这是源项目的设计意图。
_VALID_BUDGETS = {"low", "mid", "high"}


class RemoteSemanticProvider(MemoryProvider):
    """通过 HTTP 调用远端语义记忆服务。

    base_url 通过环境变量 MEMORY_SERVICE_URL 传入（详见 agent.py）。
    V10.1 服务端实现见 scripts/mock_memory_server.py（pgvector + OpenAI embedding）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        top_k: int | None = None,
        budget: str = "mid",
        min_score: float = 0.0,
        auto_retain: bool = True,
        timeout: float = 15.0,
    ) -> None:
        """
        参数：
            base_url:    远端服务根 URL（不带尾斜杠）
            top_k:       显式召回数。设了就忽略 budget（服务端约定）
            budget:      Hindsight 风格的召回档位 low/mid/high；服务端映射为 k=2/5/10
            min_score:   余弦相似度阈值（0.0-1.0），服务端在 SQL 之后过滤
            auto_retain: False 时 sync_turn 跳过 — 不持久化对话
            timeout:     httpx 超时（V10.1 调到 15s，embedding API 可能比假向量慢一档）
        """
        self._base_url = base_url.rstrip("/")
        self._top_k = top_k
        self._budget = budget.lower() if budget else "mid"
        if self._budget not in _VALID_BUDGETS:
            logger.warning(
                "Invalid budget %r, falling back to 'mid' (valid: %s)",
                budget, sorted(_VALID_BUDGETS),
            )
            self._budget = "mid"
        self._min_score = max(0.0, min(1.0, min_score))
        self._auto_retain = auto_retain
        self._session_id: str = ""
        self._client = httpx.Client(timeout=timeout)

    # -- 标识 ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "remote_semantic"

    # -- 生命周期 --------------------------------------------------------

    def is_available(self) -> bool:
        """探活 — 检查远端 /healthz 端点。

        V10.1 服务端 /healthz 会顺手检测 DB 连通性并返回 ok=True/False，
        但这里只看 HTTP 200 即可（is_available 的语义是"能不能调"，
        不是"能不能产出有效结果"）。manager 失败时跳过本 provider。
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

        V10.1 请求体增量字段（全可选，老服务端会忽略）：
            budget=low/mid/high  → 服务端语义档位
            min_score=0.3        → 服务端 SQL 后过滤

        失败返回空串 — manager 的 prefetch_all 会跳过空结果，
        不会把空围栏注入 user message。
        """
        payload: dict[str, Any] = {
            "query": query,
            "session_id": session_id or self._session_id,
            "budget": self._budget,
            "min_score": self._min_score,
        }
        # 显式 top_k 优先（服务端约定 k 存在则忽略 budget）
        if self._top_k is not None:
            payload["k"] = self._top_k

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

        V10.1 仍是同步阻塞 — embedding API 让单次 sync 涨到约 200-500ms，
        生产实现该走后台线程 + 队列（Hindsight 的 aretain_batch 模式）。
        nano 保留同步，保持时序清晰可读。

        auto_retain=False 时跳过 — 应用方在不需要持久化时（评估、回放、
        debug）可关闭，免得污染长期记忆。
        """
        if not self._auto_retain:
            return

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
