"""
RemoteSemanticProvider — V13 后台 prefetch 预热版（V12 + 两阶段 recall）。

V13 变化（相对 V12）：
- 新增 queue_prefetch()：当轮结束后启动 daemon 线程预热下一轮 recall/reflect
- 重写 prefetch()：先消费预热缓存，冷启动时 fallback 到同步调用
- 新增 _prefetch_thread / _prefetch_result / _prefetch_lock 三个实例变量
- shutdown 增加 join prefetch 线程

V12 保留（不变）：
- 单写者线程 + queue.Queue + sentinel 模式（retain 异步写入）
- sync_turn 入队即返回，不阻塞主循环

V11 保留（不变）：
- memory_mode: context / tools / hybrid
- 三个工具：hindsight_retain / hindsight_recall / hindsight_reflect
- prefetch 支持 recall / reflect 两种方式

设计依据（对应源项目 plugins/memory/hindsight/__init__.py）：
- queue_prefetch：daemon 线程 + lock 保护缓存写入，匹配源项目 1:1
- prefetch：join(timeout=3.0) + 消费缓存 + 冷启动 fallback
- 两阶段模式让第 2 轮起 prefetch 近零延迟（后台线程已提前完成 HTTP 调用）
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
from typing import Any, Callable

import httpx

from memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

_VALID_BUDGETS = {"low", "mid", "high"}
_VALID_MODES = {"context", "tools", "hybrid"}

_WRITER_SENTINEL = object()


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
        writer_join_timeout: float = 10.0,
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

        # V12: 后台写者线程相关状态
        self._retain_queue: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._shutting_down = threading.Event()
        self._atexit_registered = False
        self._writer_join_timeout = writer_join_timeout

        # V13: 后台 prefetch 预热相关状态
        self._prefetch_thread: threading.Thread | None = None
        self._prefetch_result: str = ""
        self._prefetch_lock = threading.Lock()

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
        """优雅关闭：先停收，再 drain writer，join prefetch，最后关 client。

        步骤：
        1. set _shutting_down — 后续 sync_turn / queue_prefetch 直接丢弃
        2. put sentinel + bounded join — writer drain 完已入队的 job 后退出
        3. join prefetch 线程（如果在跑）
        4. 关 httpx client
        """
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()

        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            try:
                self._retain_queue.put(_WRITER_SENTINEL)
            except Exception:
                pass
            writer.join(timeout=self._writer_join_timeout)
            if writer.is_alive():
                logger.warning(
                    "RemoteSemantic writer did not stop within %.1fs; abandoning %d pending retain(s)",
                    self._writer_join_timeout,
                    self._retain_queue.qsize(),
                )

        prefetch_thread = self._prefetch_thread
        if prefetch_thread is not None and prefetch_thread.is_alive():
            prefetch_thread.join(timeout=5.0)

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

    # ─── V9/V13 生命周期钩子 ────────────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮前消费预热结果；冷启动时 fallback 到同步调用。

        V13 两阶段模式：
        1. join 后台 prefetch 线程（timeout=3s）
        2. 取 _prefetch_result 缓存并清空
        3. 缓存为空（冷启动 / 线程超时）→ 同步 fallback
        """
        if self._memory_mode == "tools":
            return ""
        if not self._auto_recall:
            return ""

        thread = self._prefetch_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""

        if not result:
            if self._prefetch_method == "reflect":
                result = self._do_reflect(query)
            else:
                result = self._do_recall(query)

        return result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """当轮结束后启动后台线程预热下一轮的 recall/reflect。

        守卫条件（对齐源项目）：
        - tools 模式不做隐式 prefetch
        - auto_recall 关闭时跳过
        - shutting_down 时跳过
        """
        if self._memory_mode == "tools":
            return
        if not self._auto_recall:
            return
        if self._shutting_down.is_set():
            return

        def _run():
            try:
                if self._prefetch_method == "reflect":
                    text = self._do_reflect(query)
                else:
                    text = self._do_recall(query)
                if text:
                    with self._prefetch_lock:
                        self._prefetch_result = text
            except Exception as e:
                logger.debug("RemoteSemantic queue_prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="remote-semantic-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """每轮结束后入队一个 retain job — 立即返回，不阻塞主循环。

        实际 HTTP 调用在 _writer_loop 里串行执行；服务端做知识图谱抽取。
        """
        if not self._auto_retain:
            return
        if self._shutting_down.is_set():
            return

        content = f"User: {user_content}\nAssistant: {assistant_content}"
        payload = {
            "bank_id": self._bank_id,
            "content": content,
            "document_id": session_id or self._session_id,
            "tags": self._retain_tags,
            "update_mode": "append",
        }
        self._enqueue_retain(payload)

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

    # ─── V12 异步 writer ─────────────────────────────────────────────────

    def _enqueue_retain(self, payload: dict[str, Any]) -> None:
        """把一次 retain HTTP 调用包成 job 入队。"""
        def _do_retain() -> None:
            resp = self._client.post(f"{self._base_url}/retain", json=payload)
            resp.raise_for_status()

        self._ensure_writer()
        self._register_atexit()
        self._retain_queue.put(_do_retain)

    def _ensure_writer(self) -> None:
        """Lazy 启动单写者线程。

        不在 initialize() 里启动，避免纯 tools 模式且模型从不显式 retain 的场景
        白白挂一个空闲线程。
        """
        thread = self._writer_thread
        if thread is not None and thread.is_alive():
            return
        # 上一次 shutdown 后允许新写者再次运行（重新 initialize 的场景）
        self._shutting_down.clear()
        thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="remote-semantic-writer",
        )
        self._writer_thread = thread
        thread.start()

    def _writer_loop(self) -> None:
        """串行 drain retain 队列；sentinel 触发退出。

        - get(timeout=1.0)：让线程能周期性检查 _shutting_down，避免死等
        - 单个 job 异常不杀线程 — 写者必须始终活着直到 sentinel
        - task_done 始终触发 — 让外部 queue.join() 等待可用（测试中要用）
        """
        while True:
            try:
                job: Callable[[], None] | object = self._retain_queue.get(timeout=1.0)
            except queue.Empty:
                if self._shutting_down.is_set():
                    return
                continue
            try:
                if job is _WRITER_SENTINEL:
                    return
                try:
                    job()  # type: ignore[operator]
                except Exception as exc:
                    logger.warning("RemoteSemantic retain failed: %s", exc, exc_info=True)
            finally:
                self._retain_queue.task_done()

    def _register_atexit(self) -> None:
        """注册幂等的 atexit 钩子 drain writer。

        没有这个钩子，CLI 不走 MemoryManager.shutdown_all() 直接退出时，
        in-flight retain job 会与解释器 teardown 竞态。
        """
        if self._atexit_registered:
            return
        self._atexit_registered = True
        atexit.register(self._atexit_shutdown)

    def _atexit_shutdown(self) -> None:
        if self._shutting_down.is_set():
            return
        try:
            self.shutdown()
        except Exception as exc:
            logger.debug("RemoteSemantic atexit shutdown failed: %s", exc)
