"""
Mock Memory Server — V10.1 真实后端版（pgvector + OpenAI embedding）。

相对 V10 的变化：
- 存储：进程内 dict → PostgreSQL + pgvector
- 向量：md5 假向量 → OpenAI embedding（任何 OpenAI 兼容端点）
- 端点签名兼容：/recall 新增可选 budget / min_score，旧字段 query/session_id/k 全保留

为什么仍叫 "mock"：
- 这不是 Hindsight Cloud / mem0 SaaS 级别的"语义记忆服务"
- 没有事实抽取、实体图、LLM 中间层、跨 bank 隔离、tags 过滤
- 用最小代码量演示"真正能跑的 pgvector + OpenAI embedding"这条路径

Hindsight 对照：
- Hindsight `local_embedded` 模式 = hindsight-embed 守护进程 + 自带 Postgres
- 我们这里 = Postgres（docker compose） + 单文件 FastAPI

启动流程：
    docker compose up -d                              # 起 pgvector
    uv pip install -e ".[mock-server]"                # 装 fastapi + psycopg + pgvector
    cp .env.example .env  &&  vim .env                # 填 OPENAI_API_KEY 等
    python scripts/mock_memory_server.py              # 起服务
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool
from pydantic import BaseModel
import uvicorn


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mock_memory")

load_dotenv()


# ─── 配置（全部走 .env） ──────────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://nano:nano@127.0.0.1:5432/nano_memory",
)

# Embedding 端点 — 默认与 OPENAI_BASE_URL / OPENAI_API_KEY 同源，
# 也支持单独配置（embedding 走 OpenAI、对话走 DeepSeek 的混合形态）。
EMBEDDING_BASE_URL = os.environ.get(
    "EMBEDDING_BASE_URL",
    os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)
EMBEDDING_API_KEY = os.environ.get(
    "EMBEDDING_API_KEY",
    os.environ.get("OPENAI_API_KEY", ""),
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1536"))


# ─── 全局资源：DB 连接池 + OpenAI client ────────────────────────────────────
#
# 用 psycopg_pool 而不是每次新建连接 —— FastAPI 同步路由里 cold connect
# 大约 50ms，pool 摊薄到 <1ms。pool.connection() 出借/归还在 with 块里完成。

_pool: ConnectionPool | None = None
_embedding_client: OpenAI | None = None


def _make_pool() -> ConnectionPool:
    """建池 + 每次借出连接前注册 pgvector 类型适配器。

    register_vector 让 psycopg 能把 Python list[float] 自动序列化为 vector 类型，
    也能把查询返回的 vector 解码为 numpy array。我们只用前者，后者忽略。
    """
    def _configure(conn):
        register_vector(conn)

    return ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=5,
        configure=_configure,
        kwargs={"autocommit": True},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建池 + 嗅探 OpenAI，关闭时干净释放。"""
    global _pool, _embedding_client
    logger.info("connecting to db: %s", DATABASE_URL.split("@")[-1])
    _pool = _make_pool()
    _pool.open()
    _pool.wait()  # 阻塞到至少一条连接就绪 — 失败时这里就抛

    logger.info(
        "embedding endpoint: %s (model=%s, dim=%d)",
        EMBEDDING_BASE_URL,
        EMBEDDING_MODEL,
        EMBEDDING_DIM,
    )
    _embedding_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)

    yield

    logger.info("shutting down")
    if _pool is not None:
        _pool.close()


# ─── 真实 Embedding（OpenAI 范式） ───────────────────────────────────────────


def embed(text: str) -> list[float]:
    """单条 embedding。

    OpenAI 兼容端点统一走 client.embeddings.create — DeepSeek、SiliconFlow、
    本地 vLLM/llama.cpp 都能直接换 base_url 接入，是 v10.1 "openai 范式" 的体现。

    text-embedding-3 系列支持 `dimensions` 参数做 Matryoshka 截断（截 512 / 256），
    切换维度时必须同步重建 pgvector 表（VECTOR(1536) → VECTOR(512)）。
    我们暴露 EMBEDDING_DIM env，但要求 init.sql 维度与之一致 —— 不做运行时校验。
    """
    if _embedding_client is None:
        raise RuntimeError("embedding client not initialized")

    kwargs: dict[str, Any] = {"model": EMBEDDING_MODEL, "input": text}
    # 仅当 EMBEDDING_DIM != 默认时显式传 dimensions —
    # 兼容端点（如老版 DeepSeek embedding）不认这个参数。
    if EMBEDDING_DIM != 1536:
        kwargs["dimensions"] = EMBEDDING_DIM

    resp = _embedding_client.embeddings.create(**kwargs)
    return resp.data[0].embedding


# ─── 请求/响应模型 ───────────────────────────────────────────────────────────
#
# 请求体保持向下兼容 V10：query/session_id/k 仍可用，新增字段都是可选。


# budget → k 映射（参考 Hindsight 的 recall_budget=low/mid/high）
_BUDGET_TO_K = {"low": 2, "mid": 5, "high": 10}


class RecallRequest(BaseModel):
    query: str
    session_id: str = ""
    k: int | None = None             # 显式 k 优先于 budget
    budget: str | None = None        # Hindsight 路线：low / mid / high
    min_score: float = 0.0           # 余弦相似度阈值，低于则丢弃


class RecallHit(BaseModel):
    text: str
    score: float


class RecallResponse(BaseModel):
    hits: list[RecallHit]


class SyncRequest(BaseModel):
    user: str
    assistant: str
    session_id: str = ""


class SyncResponse(BaseModel):
    stored: int
    total: int


# ─── 应用 ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Nano Memory v10.1 (pgvector + OpenAI)", version="0.1.1", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """探活 + 资源状态。

    Provider 端只看 200/非 200，但人工调试时这里的 db_connected /
    embedding_dim 能快速定位"端点起来了但 DB 没连上"的情形。
    """
    db_ok = False
    total_rows = -1
    try:
        if _pool is not None:
            with _pool.connection() as conn:
                cur = conn.execute("SELECT count(*) FROM memories;")
                total_rows = cur.fetchone()[0]
            db_ok = True
    except Exception as e:
        logger.warning("healthz db probe failed: %s", e)

    return {
        "ok": db_ok,
        "db_connected": db_ok,
        "total_rows": total_rows,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
    }


def _resolve_k(req: RecallRequest) -> int:
    """显式 k > budget 映射 > 默认 5。"""
    if req.k is not None and req.k > 0:
        return req.k
    if req.budget:
        return _BUDGET_TO_K.get(req.budget.lower(), 5)
    return 5


@app.post("/recall", response_model=RecallResponse)
def recall(req: RecallRequest) -> RecallResponse:
    """SQL 下推语义检索 — 对应 Provider.prefetch()。

    pgvector 的 `<=>` 是 cosine distance（0=同向，2=反向），1 - distance = 相似度。
    在 SQL 层 ORDER BY + LIMIT 完成 top-k，应用层只过 min_score 阈值 —
    迁移到任何兼容 pgvector 的真实库（Supabase、Neon、RDS）SQL 一行不改。
    """
    if _pool is None:
        raise HTTPException(500, "db pool not initialized")

    k = _resolve_k(req)
    try:
        q_vec = embed(req.query)
    except Exception as e:
        logger.warning("embed query failed: %s", e)
        raise HTTPException(502, f"embedding failed: {e}")

    with _pool.connection() as conn:
        cur = conn.execute(
            """
            SELECT text, 1 - (embedding <=> %s::vector) AS score
            FROM memories
            WHERE session_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
            """,
            (q_vec, req.session_id, q_vec, k),
        )
        rows = cur.fetchall()

    hits = [
        RecallHit(text=text, score=float(score))
        for text, score in rows
        if float(score) >= req.min_score
    ]
    logger.info(
        "recall session=%s query=%r k=%d min_score=%.2f → %d/%d hits",
        req.session_id, req.query, k, req.min_score, len(hits), len(rows),
    )
    return RecallResponse(hits=hits)


@app.post("/sync", response_model=SyncResponse)
def sync(req: SyncRequest) -> SyncResponse:
    """持久化一轮对话 — 对应 Provider.sync_turn()。

    策略沿用 V10：把 user / assistant 拼成单条记忆。生产实现可能拆开、做摘要、
    或者只 retain 有信息密度的内容（Hindsight 的 retain_async + 实体抽取）。
    """
    if _pool is None:
        raise HTTPException(500, "db pool not initialized")

    text = f"User: {req.user.strip()}\nAssistant: {req.assistant.strip()}"
    try:
        vec = embed(text)
    except Exception as e:
        logger.warning("embed sync failed: %s", e)
        raise HTTPException(502, f"embedding failed: {e}")

    with _pool.connection() as conn:
        conn.execute(
            "INSERT INTO memories (session_id, text, embedding) VALUES (%s, %s, %s);",
            (req.session_id, text, vec),
        )
        cur = conn.execute(
            "SELECT count(*) FROM memories WHERE session_id = %s;",
            (req.session_id,),
        )
        total = cur.fetchone()[0]

    logger.info("sync session=%s total=%d", req.session_id, total)
    return SyncResponse(stored=1, total=total)


# ─── 入口 ────────────────────────────────────────────────────────────────────


def main() -> None:
    host = os.environ.get("MOCK_MEMORY_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_MEMORY_PORT", "8765"))
    logger.info("starting mock memory server on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
