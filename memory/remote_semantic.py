"""
RemoteSemanticProvider — V11 知识图谱版（Hindsight 1:1 复现）。

V11 变化（相对 V10.1）：
- 支持 memory_mode: context / tools / hybrid
- 暴露 hindsight_retain / hindsight_recall / hindsight_reflect 三个工具
- sync_turn 调 /retain（服务端做实体/关系/事实抽取）
- prefetch 支持 recall / reflect 两种方式

对应源项目：plugins/memory/hindsight/__init__.py
- context 模式 = 纯 prefetch 注入（模型不感知记忆工具）
- tools 模式 = 暴露工具让模型主动调用（不自动 prefetch）
- hybrid 模式 = 两者并存（自动 prefetch + 模型可主动调用）
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

_VALID_BUDGETS = {"low", "mid", "high"}
_VALID_MODES = {"context", "tools", "hybrid"}


# ─── Tool Schemas（对齐源项目 Hindsight 命名）────────────────────────────────

RETAIN_SCHEMA = {
    "name": "hindsight_retain",
    "description": (
        "Store information to long-term memory. The server automatically "
        "extracts entities, relations, and facts for knowledge graph storage."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The information to store in long-term memory.",
            },
            "context": {
                "type": "string",
                "description": "Short label for categorization (e.g. 'user preference', 'project decision').",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering during recall.",
            },
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "hindsight_recall",
    "description": (
        "Search long-term memory using semantic search, entity matching, "
        "and knowledge graph traversal. Returns ranked results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for in long-term memory.",
            },
        },
        "required": ["query"],
    },
}

REFLECT_SCHEMA = {
    "name": "hindsight_reflect",
    "description": (
        "Synthesize a reasoned answer from long-term memories. Unlike recall, "
        "this reasons across all stored knowledge to produce a coherent response."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The question to reflect on using long-term memory.",
            },
        },
        "required": ["query"],
    },
}


class RemoteSemanticProvider(MemoryProvider):
    """通过 HTTP 调用远端知识图谱记忆服务（Hindsight 模式）。

    支持三种 memory_mode：
    - context: 纯 prefetch 注入，模型不感知记忆工具
    - tools: 暴露 retain/recall/reflect 工具，模型主动调用
    - hybrid: 两者并存（自动 prefetch + 模型可主动调用）
    """

    def __init__(
        self,
        base_url: str,
        *,
        bank_id: str = "hermes",
        budget: str = "mid",
        memory_mode: str = "hybrid",
        prefetch_method: str = "recall",
        auto_retain: bool = True,
        auto_recall: bool = True,
        retain_tags: list[str] | None = None,
        timeout: float = 360.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bank_id = bank_id
        self._budget = budget.lower() if budget else "mid"
        if self._budget not in _VALID_BUDGETS:
            logger.warning("Invalid budget %r, falling back to 'mid'", budget)
            self._budget = "mid"
        self._memory_mode = memory_mode.lower() if memory_mode else "hybrid"
        if self._memory_mode not in _VALID_MODES:
            logger.warning("Invalid memory_mode %r, falling back to 'hybrid'", memory_mode)
            self._memory_mode = "hybrid"
        self._prefetch_method = prefetch_method.lower()
        self._auto_retain = auto_retain
        self._auto_recall = auto_recall
        self._retain_tags = retain_tags or []
        self._session_id: str = ""
        self._client = httpx.Client(timeout=timeout)

    @property
    def name(self) -> str:
        return "remote_semantic"

    # ─── 生命周期 ────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        try:
            resp = self._client.get(f"{self._base_url}/healthz")
            return resp.status_code == 200
        except Exception as e:
            logger.warning("RemoteSemantic /healthz failed: %s", e)
            return False

    def initialize(self, session_id: str = "", **kwargs) -> None:
        self._session_id = session_id

    def shutdown(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ─── prompt / 工具 ───────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        if self._memory_mode == "tools":
            return (
                "You have access to long-term memory tools (hindsight_retain, "
                "hindsight_recall, hindsight_reflect). Use them to store and "
                "retrieve important information across sessions."
            )
        return "Long-term semantic memory is available; relevant context will be recalled per turn."

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """根据 memory_mode 返回工具 schema。

        - context 模式：不暴露工具（纯隐式 prefetch）
        - tools / hybrid 模式：暴露 retain/recall/reflect 三个工具
        """
        if self._memory_mode == "context":
            return []
        return [RETAIN_SCHEMA, RECALL_SCHEMA, REFLECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """路由工具调用到对应 HTTP 端点。"""
        if tool_name == "hindsight_retain":
            return self._tool_retain(args)
        elif tool_name == "hindsight_recall":
            return self._tool_recall(args)
        elif tool_name == "hindsight_reflect":
            return self._tool_reflect(args)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # ─── V9 生命周期钩子 ─────────────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮前自动召回（context / hybrid 模式生效）。"""
        if self._memory_mode == "tools":
            return ""
        if not self._auto_recall:
            return ""

        if self._prefetch_method == "reflect":
            return self._do_reflect(query)
        return self._do_recall(query)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """每轮结束后调 /retain — 服务端做知识图谱抽取。"""
        if not self._auto_retain:
            return

        content = f"User: {user_content}\nAssistant: {assistant_content}"
        payload = {
            "bank_id": self._bank_id,
            "content": content,
            "document_id": session_id or self._session_id,
            "tags": self._retain_tags,
            "update_mode": "append",
        }
        try:
            resp = self._client.post(f"{self._base_url}/retain", json=payload)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("RemoteSemantic /retain failed: %s", e)

    # ─── 内部方法 ────────────────────────────────────────────────────────

    def _do_recall(self, query: str) -> str:
        """调 /recall 端点，格式化返回。"""
        payload = {
            "bank_id": self._bank_id,
            "query": query,
            "budget": self._budget,
        }
        try:
            resp = self._client.post(f"{self._base_url}/recall", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("RemoteSemantic /recall failed: %s", e)
            return ""

        results = data.get("results") or []
        if not results:
            return ""

        lines = []
        for i, hit in enumerate(results, 1):
            text = (hit.get("text") or "").strip()
            score = hit.get("score")
            source = hit.get("source", "")
            if not text:
                continue
            prefix = f"[{i}]"
            if isinstance(score, (int, float)):
                prefix += f" (score={score:.3f}"
                if source:
                    prefix += f", {source}"
                prefix += ")"
            lines.append(f"{prefix} {text}")
        return "\n".join(lines)

    def _do_reflect(self, query: str) -> str:
        """调 /reflect 端点，返回合成文本。"""
        payload = {
            "bank_id": self._bank_id,
            "query": query,
            "budget": self._budget,
        }
        try:
            resp = self._client.post(f"{self._base_url}/reflect", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("RemoteSemantic /reflect failed: %s", e)
            return ""
        return data.get("text", "")

    def _tool_retain(self, args: dict[str, Any]) -> str:
        """hindsight_retain 工具实现。"""
        payload = {
            "bank_id": self._bank_id,
            "content": args.get("content", ""),
            "context": args.get("context", ""),
            "document_id": self._session_id,
            "tags": args.get("tags", []) + self._retain_tags,
        }
        try:
            resp = self._client.post(f"{self._base_url}/retain", json=payload)
            resp.raise_for_status()
            return json.dumps(resp.json(), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _tool_recall(self, args: dict[str, Any]) -> str:
        """hindsight_recall 工具实现。"""
        payload = {
            "bank_id": self._bank_id,
            "query": args.get("query", ""),
            "budget": self._budget,
        }
        try:
            resp = self._client.post(f"{self._base_url}/recall", json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return json.dumps({"results": [], "message": "No relevant memories found."})
            return json.dumps(data, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _tool_reflect(self, args: dict[str, Any]) -> str:
        """hindsight_reflect 工具实现。"""
        payload = {
            "bank_id": self._bank_id,
            "query": args.get("query", ""),
            "budget": self._budget,
        }
        try:
            resp = self._client.post(f"{self._base_url}/reflect", json=payload)
            resp.raise_for_status()
            return json.dumps(resp.json(), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})
