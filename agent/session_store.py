"""V24.0/V24.1 — 会话状态持久化（SQLite 会话子系统 + 真 resume + 压缩链）。

这一档解决 v14 的 **假 resume** —— v14 只落了 ``on_session_switch`` 生命周期
钩子（给 memory manager 切 bank key），**从没把对话 messages 存过盘**。结果
``/resume <id>`` 只换了 memory bank 的 key、把对话清成只剩 system prompt，
拿不回任何历史；进程一退，整段对话蒸发。

本模块让 ``messages`` 成为可持久化的真实实体：
- 每轮末 / 退出时 ``append`` 到 SQLite（``sessions/state.db``）
- ``/resume <id>`` 真正 ``load`` 回历史对话
- 压缩点会话分裂，压缩前全文封存在旧 session、可回溯（V24.1）

**V24.0 → V24.1 演进**（同一文件两档）：
- V24.0：``save`` 全量删重插 + 真 resume + ``/sessions`` —— 独立修掉假 resume。
- V24.1：新增 ``append`` 走 append-only 游标（只增不删）+ 压缩链三件套
  （``end_session`` / ``create_session`` / ``resolve_resume_tip``）+ ``list_sessions``
  折叠。生产路径切到 ``append``；``save`` 保留（v24.0 测试 + 全量重写语义仍可用）。
  为什么必须从全量重写改起：全量删重插会让压缩后旧消息被覆盖丢失，与"压缩前
  可回溯"冲突；append-only 只增不删才是源项目"压缩前历史永久留旧 session"的根因。

源项目对照：``hermes-agent/hermes_state.py``（SQLite 会话存储核心，
SCHEMA_VERSION=11、30+ 字段、WAL + FTS5 + append-only 游标 + 压缩链）。
nano 取最小够用切片：

| 维度 | 源项目 | nano v24.1 |
|------|--------|-----------|
| 后端 | SQLite + WAL + FTS5 三元组 CJK | SQLite + WAL（无 FTS）|
| 写入 | append-only（游标只追加） | **append-only**（``COUNT(*)`` 当无状态游标）|
| 会话表 | 30+ 字段 | 13 字段（4 维 token 摊平 + 压缩链 3 字段）|
| Resume | resolve 沿链重定向（root 有消息则短路） | **无条件走到 tip**（教学版要"resume=压缩后最新点"）|
| 压缩 | 会话分裂 + parent 串链 + ``started_at>=ended_at`` 判压缩子 | 会话分裂 + parent 串链（库只含压缩链，无需判别）|

**schema 一次建全**（v24.0 即含 ``parent_session_id`` / ``ended_at`` /
``end_reason``）—— v24.1 只是开始往这些列写值，不改表。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# 默认库放 nano 根的 ``sessions/`` 下；env ``SESSION_DB_PATH`` 覆盖。
# ``:memory:`` 走纯内存库（测试 / 关闭持久化用）。
DB_PATH = Path(os.environ.get("SESSION_DB_PATH", "sessions/state.db"))

# 4 维 token 列名 —— 与 ``agent.runtime.SESSION_TOKEN_KEYS`` 对齐（input /
# output / cache_read / cache_write），摊平进 4 个 INTEGER 列。
_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# 建表 DDL —— 源项目 30+ 字段裁到必要的 + 压缩链 3 字段。
# 逐条入行（messages 一行一条）而非整段 JSON 塞一列：对齐源项目 messages 表，
# 教"把 OpenAI 消息规范化进表 + 读时重建 shape"这个真实生产模式；也为
# append-only 游标（COUNT(*)）和后续可选 FTS 留地基。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    created_at    REAL,
    updated_at    REAL,
    turn_count    INTEGER,
    model         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    parent_session_id  TEXT,
    ended_at      REAL,
    end_reason    TEXT,
    title         TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT,
    tool_call_id  TEXT,
    tool_calls    TEXT,
    reasoning_content TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
"""


class SessionStore:
    """SQLite 驱动的会话存储 —— v24.0 全量删重插形态。

    线程模型：``sqlite3`` 连接非线程安全 → 只在父 main loop 单线程用。
    delegate 子线程 **不碰** store（子结果已在父 messages 里，随父会话一起落盘，
    见计划决策 8）。所以 ``check_same_thread`` 用默认 True 即可。

    并发写保护（决策 6）：WAL + busy_timeout，防物理损坏不防逻辑覆盖。
    多窗口共享同一 ``state.db`` 时，两窗口 ``/resume`` 同一 ``session_id`` 各聊
    各的会 last-write-wins（后 commit 覆盖先 commit）。nano 单用户教学版不引入
    会话级锁，约定每窗口用不同 ``MEMORY_SESSION_ID`` 物理隔离。
    """

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = db_path
        # ":memory:" 不建父目录；文件库才 mkdir
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        # WAL 读不挡写；busy_timeout 是手写重试循环的内置替身（第二个写者等 5s）
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── 写入 ────────────────────────────────────────────────────────────
    def save(
        self,
        session_id: str,
        messages: list[dict],
        *,
        turn_count: int = 0,
        model: str = "",
        session_tokens: Optional[dict[str, int]] = None,
    ) -> None:
        """v24.0：全量删重插。一个事务里 DELETE 旧 messages + 重插当前视图。

        剔除首条 system message（决策 3：system prompt 随启动环境变化，resume
        时用 ``prompt_builder.build()`` 重建，不存旧的）。

        ``created_at`` 保旧值（首次 save 才写），``updated_at`` 每次刷新 —— 让
        ``list_sessions`` 能按"最近活跃"倒序。

        v24.1 会把本方法改名 ``append`` 走游标增量；v24.0 全量重写是过渡形态，
        足以独立修掉假 resume。
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        persistable = [m for m in messages if m.get("role") != "system"]
        now = time.time()
        tokens = session_tokens or {}
        with self.conn:  # 事务：任一 INSERT 抛异常则整次回滚，不留半截
            row = self.conn.execute(
                "SELECT created_at FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            created_at = row["created_at"] if row else now
            self.conn.execute(
                "DELETE FROM messages WHERE session_id=?", (session_id,)
            )
            for seq, msg in enumerate(persistable):
                self._insert_message(session_id, seq, msg)
            self._upsert_session(
                session_id, created_at, now, turn_count, model, tokens
            )

    def append(
        self,
        session_id: str,
        messages: list[dict],
        *,
        turn_count: int = 0,
        model: str = "",
        session_tokens: Optional[dict[str, int]] = None,
    ) -> None:
        """V24.1 append-only：只追加"游标之后"的新消息，**绝不 DELETE 旧消息**。

        游标无状态 = ``COUNT(*) WHERE session_id``（已写条数）。剔除 system 后，
        索引 ≥ 游标的消息才插 —— 重启 / resume 后游标自动正确，不在内存存
        ``_last_flushed_idx``。

        与 v24.0 ``save`` 的本质差异：``save`` 全量删重插（in-place 压缩让
        messages 变短时，旧消息被覆盖丢失）；``append`` 只增不删，所以压缩前的
        完整历史永久留在旧 session 行里 —— 这正是决策 2/7 "压缩前可回溯"的根因。

        与 in-place 压缩的冲突靠会话分裂化解（决策 7）：压缩点 ``end_session`` +
        ``create_session`` 换 ``session_id``，新 session ``COUNT(*)=0`` → 游标归零，
        下一轮从 seq 0 起插压缩后视图。每个 ``session_id`` 的 messages 只增不减。

        ``persistable`` 短于已写条数时（理论上不该发生 —— 同一 session 不该缩，
        缩了说明该走分裂换 id），``range`` 为空 → no-op，旧行原样保留。
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        persistable = [m for m in messages if m.get("role") != "system"]
        now = time.time()
        tokens = session_tokens or {}
        with self.conn:  # 事务：游标读 + 增量插 + upsert 元数据一致提交
            row = self.conn.execute(
                "SELECT created_at FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            created_at = row["created_at"] if row else now
            already = self.conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()["n"]
            for seq in range(already, len(persistable)):
                self._insert_message(session_id, seq, persistable[seq])
            self._upsert_session(
                session_id, created_at, now, turn_count, model, tokens
            )

    def end_session(self, session_id: str, reason: str) -> None:
        """V24.1：给会话打 ``ended_at`` + ``end_reason`` 标，**不动 messages**。

        压缩分裂的第二步：封存旧 session。封存后旧 session 仍能 ``load`` 出全文 ——
        这是"压缩前可回溯"的核心（决策 7）。会话不存在则 UPDATE 影响 0 行（no-op）。
        """
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE session_id=?",
                (time.time(), reason, session_id),
            )

    def create_session(
        self,
        session_id: str,
        *,
        parent_session_id: Optional[str] = None,
        model: str = "",
        turn_count: int = 0,
    ) -> None:
        """V24.1：建压缩分裂的子 session 行。

        游标天然为 0（新行无 messages）→ 下一轮 ``append`` 从 seq 0 起插压缩后视图。
        ``parent_session_id`` 把子串到旧 session 上，构成压缩链。nano 库只含压缩链
        （delegate 子不入库，决策 8），故任何 ``parent_session_id`` 都是压缩链接 ——
        无需源项目 ``started_at >= ended_at`` 那种"压缩子 vs delegate 子"的判别。
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at, "
                "turn_count, model, parent_session_id) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "parent_session_id=excluded.parent_session_id",
                (session_id, now, now, turn_count, model, parent_session_id),
            )

    def resolve_resume_tip(self, session_id: str) -> str:
        """V24.1：沿 ``parent_session_id`` 往子代走到压缩链最末 tip。

        让"一条逻辑对话"默认恢复到压缩后的最新连续点，而非停在压缩断点的旧
        root（旧 root 虽留着压缩前全文，但 resume 它会拿回超长对话、下一轮立刻
        重压）。无子代的普通 / tip 会话原样返回。深度上限 32 防环。

        **与源项目的取舍**：源 ``resolve_resume_session_id`` 在"root 自己有消息"
        时短路返回 root；nano 改用 ``get_compression_tip`` 那种无条件走到 tip 的
        语义 —— 教学版要的就是"resume = 压缩后的最新点"，且 nano 库只有压缩链、
        不会把 delegate 子混进来误导走向。压缩前 root 默认对用户隐藏（列表折叠），
        想回溯走 ``load(root_id)`` 直读或 ``/sessions --all``。
        """
        if not session_id:
            return session_id
        current = session_id
        seen = {current}
        for _ in range(32):
            child = self.conn.execute(
                "SELECT session_id FROM sessions WHERE parent_session_id=? "
                "ORDER BY created_at DESC, session_id DESC LIMIT 1",
                (current,),
            ).fetchone()
            if child is None:
                return current
            cid = child["session_id"]
            if not cid or cid in seen:
                return current
            seen.add(cid)
            current = cid
        return current

    def _chain_root(self, session_id: str) -> str:
        """沿 ``parent_session_id`` 往上走到压缩链 root（折叠列表取 origin 预览用）。"""
        current = session_id
        seen = {current}
        for _ in range(32):
            row = self.conn.execute(
                "SELECT parent_session_id FROM sessions WHERE session_id=?", (current,)
            ).fetchone()
            if row is None or not row["parent_session_id"]:
                return current
            parent = row["parent_session_id"]
            if parent in seen:
                return current
            seen.add(parent)
            current = parent
        return current

    def _insert_message(self, session_id: str, seq: int, msg: dict) -> None:
        """把一条 OpenAI 消息规范化进 messages 表。

        ``tool_calls`` 是 list → 存 JSON 字符串（仅 assistant 有调用时非空）；
        ``tool_call_id`` 仅 role=tool；``content`` / ``reasoning_content`` 非空才存。
        与 ``transports.types.build_assistant_history_msg`` 的产物对称。
        """
        tool_calls = msg.get("tool_calls")
        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        self.conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, "
            "tool_call_id, tool_calls, reasoning_content) VALUES (?,?,?,?,?,?,?)",
            (
                session_id,
                seq,
                msg.get("role"),
                msg.get("content"),
                msg.get("tool_call_id"),
                tool_calls_json,
                msg.get("reasoning_content"),
            ),
        )

    def _upsert_session(
        self,
        session_id: str,
        created_at: float,
        updated_at: float,
        turn_count: int,
        model: str,
        tokens: dict[str, int],
    ) -> None:
        self.conn.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at, turn_count, "
            "model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "updated_at=excluded.updated_at, turn_count=excluded.turn_count, "
            "model=excluded.model, input_tokens=excluded.input_tokens, "
            "output_tokens=excluded.output_tokens, "
            "cache_read_tokens=excluded.cache_read_tokens, "
            "cache_write_tokens=excluded.cache_write_tokens",
            (
                session_id,
                created_at,
                updated_at,
                turn_count,
                model,
                int(tokens.get("input", 0)),
                int(tokens.get("output", 0)),
                int(tokens.get("cache_read", 0)),
                int(tokens.get("cache_write", 0)),
            ),
        )

    # ── 读取 ────────────────────────────────────────────────────────────
    def load(self, session_id: str) -> Optional[dict]:
        """读回一个会话，重建 OpenAI messages shape（**不含 system**）。

        返回 ``{"messages": [...], "turn_count": int, "model": str,
        "session_tokens": {4 维}, "created_at": float, "updated_at": float}``；
        会话不存在则 ``None``。

        重建规则与 ``_insert_message`` 对称：``tool_calls`` 列非空才 ``json.loads``
        挂上、``tool_call_id`` 仅 role=tool 带、``content`` / ``reasoning_content``
        非空才带。调用方负责在 messages 前重建 system prompt。
        """
        sess = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if sess is None:
            return None
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        messages = [self._row_to_message(r) for r in rows]
        return {
            "messages": messages,
            "turn_count": sess["turn_count"] or 0,
            "model": sess["model"] or "",
            "session_tokens": {
                "input": sess["input_tokens"] or 0,
                "output": sess["output_tokens"] or 0,
                "cache_read": sess["cache_read_tokens"] or 0,
                "cache_write": sess["cache_write_tokens"] or 0,
            },
            "created_at": sess["created_at"],
            "updated_at": sess["updated_at"],
        }

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        """单行还原成 OpenAI 消息 dict —— 只挂非空字段，保持 shape 干净。"""
        msg: dict[str, Any] = {"role": row["role"]}
        # content 即使空串也带上（assistant 占位 " " 是合法历史）；仅 None 时省略
        if row["content"] is not None:
            msg["content"] = row["content"]
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        if row["tool_calls"]:
            msg["tool_calls"] = json.loads(row["tool_calls"])
        if row["reasoning_content"] is not None:
            msg["reasoning_content"] = row["reasoning_content"]
        return msg

    def list_sessions(self, fold_chains: bool = True) -> list[dict]:
        """列出会话元数据，按 ``updated_at`` 倒序（最近活跃在前）。

        每条带 ``msg_count`` 和首条 user 消息的 ``preview``（截断 60 字），
        供 ``/sessions`` 渲染 ``<id>  <turn>turns  <n>msgs  <updated>  <preview>``。

        ``fold_chains=True``（默认，V24.1）：压缩链折叠 —— 凡是别人的
        ``parent_session_id`` 指向的会话（链中被压缩封存的 root / 中间节点）一律
        隐藏，只留 tip 和无链的独立会话。一条逻辑对话 = 一行，与源项目
        ``project_compression_tips=True`` 同义。tip 的 ``preview`` 取自链 root 的
        首条 user 消息（原始第一问，而非压缩摘要 —— 用户认得出是哪段对话）。

        ``fold_chains=False``（``/sessions --all`` / debug）：展开所有节点，含压缩
        前 root —— 才看得到被折叠藏起来的压缩前原文节点。
        """
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        hidden: set[str] = set()
        if fold_chains:
            # 任何被引用为 parent 的会话 = 链中非 tip 节点 → 折叠时隐藏
            parent_rows = self.conn.execute(
                "SELECT DISTINCT parent_session_id AS p FROM sessions "
                "WHERE parent_session_id IS NOT NULL"
            ).fetchall()
            hidden = {r["p"] for r in parent_rows}
        result: list[dict] = []
        for sess in rows:
            sid = sess["session_id"]
            if sid in hidden:
                continue
            count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (sid,)
            ).fetchone()["n"]
            # 折叠时 preview 取链 root 的首问；展开时取本节点自己的首问
            preview_sid = self._chain_root(sid) if fold_chains else sid
            preview_row = self.conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content IS NOT NULL ORDER BY seq LIMIT 1",
                (preview_sid,),
            ).fetchone()
            preview = (preview_row["content"] if preview_row else "") or ""
            preview = preview.replace("\n", " ").strip()
            if len(preview) > 60:
                preview = preview[:60] + "..."
            result.append({
                "session_id": sid,
                "turn_count": sess["turn_count"] or 0,
                "msg_count": count,
                "model": sess["model"] or "",
                "updated_at": sess["updated_at"],
                "created_at": sess["created_at"],
                "parent_session_id": sess["parent_session_id"],
                "preview": preview,
            })
        return result

    def delete(self, session_id: str) -> bool:
        """删除一个会话的 session 行 + 全部 messages。返回是否真删到行。"""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM sessions WHERE session_id=?", (session_id,)
            )
            self.conn.execute(
                "DELETE FROM messages WHERE session_id=?", (session_id,)
            )
        return cur.rowcount > 0

    def close(self) -> None:
        """关连接 —— WAL 在最后一个连接关闭时自动 checkpoint 回主库。"""
        self.conn.close()
