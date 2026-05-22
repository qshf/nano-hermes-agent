"""
RemoteSemanticProvider — V16 retain 批量 + cadence 控制版。

V16 变化（相对 V14/V15）：
- 新增 retain_every_n_turns：N>1 时把多轮 turn 缓冲后合并为一次 retain
  - 减少服务端 LLM 抽取次数（每次 retain 都要做实体/关系/事实抽取）
  - 提高知识图谱一致性（一组连续 turn 一次性进入抽取上下文）
- 实例缓冲：_session_turns（list[str] turn json）+ _turn_counter
- on_session_switch：drain 前先 flush 旧 buffer，再清空 + 轮转
- shutdown：drain 前 flush buffer
- 对应源项目 plugins/memory/hindsight/__init__.py 的 retain_every_n_turns 字段

V15 保留：
- on_pre_compress() 抢救压缩前对话

V14 保留：
- on_session_switch()：drain writer queue + 清缓存 + 轮转 session_id
- 切换后 writer 线程保持存活供新 session 复用

V13 保留：queue_prefetch 两阶段预热
V12 保留：单写者线程 + queue.Queue + sentinel
V11 保留：memory_mode + hindsight 三工具

设计依据（对应源项目 plugins/memory/hindsight/__init__.py）：
- retain_every_n_turns：与源 1:1，counter % N != 0 缓冲，等于 0 才入队
- on_session_switch：drain 前 flush buffer，避免旧 session 缓冲数据丢失
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
        retain_every_n_turns: int = 1,
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
        self._retain_every_n_turns = max(1, int(retain_every_n_turns))
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

        # V16: retain 批量缓冲 — N>1 时在内存累积 turn 直到达到阈值才入队
        self._session_turns: list[str] = []
        self._turn_counter: int = 0
        self._buffer_lock = threading.Lock()

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
        """优雅关闭：flush buffer → 停收 → drain writer → join prefetch → 关 client。

        步骤：
        1. flush 缓冲 turn — 否则 retain_every_n_turns>1 时会丢数据
        2. set _shutting_down — 后续 sync_turn / queue_prefetch 直接丢弃
        3. put sentinel + bounded join — writer drain 完已入队的 job 后退出
        4. join prefetch 线程（如果在跑）
        5. 关 httpx client
        """
        if self._shutting_down.is_set():
            return

        # 必须先 flush，set 标志后 _enqueue_retain 会被 sync_turn 跳过；
        # 这里走的是直接 put 到 queue 的路径，不受 _shutting_down 影响。
        self._flush_buffer()
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

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        reset: bool = False,
        **kwargs,
    ) -> None:
        """切换会话 — flush buffer + drain writer + 清 prefetch 缓存 + 轮转 session_id。

        5 步：
        1. flush 旧 session 的缓冲 turn — 否则 retain_every_n_turns>1 时会丢数据
        2. drain writer queue — 旧 session 的 retain 必须全部落盘
        3. join prefetch thread + 清空缓存 — 旧 session 的预热对新 session 无意义
        4. 轮转 session_id + 重置 turn_counter
        5. log transition

        注意：不 set _shutting_down — writer 线程保持存活供新 session 复用。
        """
        old_session_id = self._session_id

        flushed = self._flush_buffer(session_id=old_session_id)

        self._retain_queue.join()

        prefetch_thread = self._prefetch_thread
        if prefetch_thread is not None and prefetch_thread.is_alive():
            prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            self._prefetch_result = ""
        self._prefetch_thread = None

        self._session_id = new_session_id
        with self._buffer_lock:
            self._session_turns = []
            self._turn_counter = 0

        logger.info(
            "RemoteSemantic session switch: %s → %s (reset=%s, flushed=%d turns)",
            old_session_id,
            new_session_id,
            reset,
            flushed,
        )

    def on_pre_compress(self, messages: list[dict], **kwargs) -> None:
        """上下文压缩前抢救 — 提取即将被丢弃的对话，入队 retain 到知识图谱。

        从被压缩的消息中提取最后 10 条 user/assistant 对话，
        拼接后通过 writer queue 异步 retain（非阻塞）。
        """
        if self._shutting_down.is_set():
            return

        parts = []
        for msg in messages[-10:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
                parts.append(f"{role.capitalize()}: {content[:500]}")

        if not parts:
            return

        combined = "\n".join(parts)
        payload = {
            "bank_id": self._bank_id,
            "content": f"[Pre-compression context]\n{combined}",
            "document_id": self._session_id,
            "tags": self._retain_tags + ["pre-compress"],
            "update_mode": "append",
        }
        self._enqueue_retain(payload)
        logger.info("RemoteSemantic on_pre_compress: enqueued %d messages for retain", len(parts))

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """每轮结束后缓冲 turn；达到 retain_every_n_turns 才入队 retain job。

        N=1（默认）：行为与 V12-V15 一致，每轮立即入队。
        N>1：累积到第 N 轮才把 N 条 turn 合并成一条 content 入队，
            服务端的 LLM 抽取看到更完整的对话上下文，且抽取调用量降为 1/N。

        实际 HTTP 调用在 _writer_loop 里串行执行；服务端做知识图谱抽取。
        """
        if not self._auto_retain:
            return
        if self._shutting_down.is_set():
            return
        if session_id:
            self._session_id = session_id

        turn_text = f"User: {user_content}\nAssistant: {assistant_content}"

        with self._buffer_lock:
            self._session_turns.append(turn_text)
            self._turn_counter += 1
            if self._turn_counter % self._retain_every_n_turns != 0:
                return
            # 达到批量阈值 — snapshot 后清空缓冲
            turns_snapshot = list(self._session_turns)
            self._session_turns = []

        self._enqueue_buffered_retain(turns_snapshot, session_id=self._session_id)

    def _enqueue_buffered_retain(self, turns: list[str], *, session_id: str) -> None:
        """把一组缓冲的 turn 合并为一条 content 入队（带 v16 batch tag）。"""
        if not turns:
            return
        content = "\n\n---\n\n".join(turns) if len(turns) > 1 else turns[0]
        tags = list(self._retain_tags)
        if len(turns) > 1:
            tags.append(f"batch:{len(turns)}")
        payload = {
            "bank_id": self._bank_id,
            "content": content,
            "document_id": session_id or self._session_id,
            "tags": tags,
            "update_mode": "append",
        }
        self._enqueue_retain(payload)

    def _flush_buffer(self, *, session_id: str = "") -> int:
        """把当前缓冲的 turn 立即入队（用于 session switch / shutdown 前）。

        返回入队的 turn 数。N=1 时缓冲始终为空，返回 0。
        """
        with self._buffer_lock:
            if not self._session_turns:
                return 0
            turns_snapshot = list(self._session_turns)
            self._session_turns = []
            self._turn_counter = 0

        self._enqueue_buffered_retain(turns_snapshot, session_id=session_id or self._session_id)
        return len(turns_snapshot)

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
