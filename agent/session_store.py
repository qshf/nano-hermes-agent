"""V24.0 — 会话状态持久化（SQLite 会话子系统 + 真 resume）。

这一档解决 v14 的 **假 resume** —— v14 只落了 ``on_session_switch`` 生命周期
钩子（给 memory manager 切 bank key），**从没把对话 messages 存过盘**。结果
``/resume <id>`` 只换了 memory bank 的 key、把对话清成只剩 system prompt，
拿不回任何历史；进程一退，整段对话蒸发。

本模块让 ``messages`` 成为可持久化的真实实体：
- 每轮末 / 退出时 ``save`` 到 SQLite（``sessions/state.db``）
- ``/resume <id>`` 真正 ``load`` 回历史对话

源项目对照：``hermes-agent/hermes_state.py``（SQLite 会话存储核心，
SCHEMA_VERSION=11、30+ 字段、WAL + FTS5 + append-only 游标 + 压缩链）。
nano v24.0 取最小够用切片：

| 维度 | 源项目 | nano v24.0 |
|------|--------|-----------|
| 后端 | SQLite + WAL + FTS5 三元组 CJK | SQLite + WAL（无 FTS）|
| 写入 | append-only（游标只追加） | **全量删重插**（决策 2：v24.0 过渡形态，v24.1 迁 append-only）|
| 会话表 | 30+ 字段 | 13 字段（4 维 token 摊平 + 压缩链 3 字段预留）|
| Resume | resolve 沿链重定向到 tip | 直接 load target（重定向归 v24.1）|
| 压缩 | 会话分裂 + parent 串链 | 不碰（归 v24.1）|

**为什么 v24.0 用全量删重插而非 append-only**（决策 2）：
append-only 与压缩链绑死 —— append-only 下一旦 in-place 压缩，游标与变短的
messages 错位，必须靠会话分裂化解。这俩进不了第一档。全量重写简单、能独立
修掉假 resume、切割面干净，先交付。v24.1 再迁 append-only + 压缩链（~30 行
返工换 v24.0 独立上线 + 里程碑清晰）。

**schema 一次建全**（含 v24.1 才写的 ``parent_session_id`` / ``ended_at`` /
``end_reason``）—— 避免 v24.1 改表。v24.0 只是不往这些列写值。
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

# 建表 DDL —— 源项目 30+ 字段裁到必要的 + 压缩链 3 字段（v24.1 才写）。
# 逐条入行（messages 一行一条）而非整段 JSON 塞一列：对齐源项目 messages 表，
# 教"把 OpenAI 消息规范化进表 + 读时重建 shape"这个真实生产模式；也为
# v24.1 的 append-only 游标（COUNT(*)）和后续可选 FTS 留地基。
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

    def list_sessions(self) -> list[dict]:
        """列出所有会话元数据，按 ``updated_at`` 倒序（最近活跃在前）。

        每条带 ``msg_count`` 和首条 user 消息的 ``preview``（截断 60 字），
        供 ``/sessions`` 渲染 ``<id>  <turn>turns  <n>msgs  <updated>  <preview>``。
        """
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        result: list[dict] = []
        for sess in rows:
            sid = sess["session_id"]
            count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (sid,)
            ).fetchone()["n"]
            preview_row = self.conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content IS NOT NULL ORDER BY seq LIMIT 1",
                (sid,),
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
