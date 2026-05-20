"""
Mock Memory Server — V10 配套的"长期记忆"HTTP 服务（教学用）。

**这不是生产级实现**。目的是验证 V9 钩子的形状（prefetch 接收什么、返回什么；
sync_turn 该在哪触发），不是真做语义搜索。

为什么不引入 chromadb / qdrant / pgvector：
- nano 项目要保持单进程可读。引入向量数据库依赖 = 多服务系统 + ML 模型加载，
  会掩盖 V9 钩子的真实形态。
- Hash-based 假向量（md5 → 16 维 float）能演示"同样文本得到同样向量"
  和"余弦相似度选 top-k"两个核心机制 — 足以演示真实时序。

替换为生产实现的路径（不改 Provider 端任何一行）：
- 这个文件 → FastAPI + sentence-transformers + asyncpg + pgvector
- /recall 改成 SELECT ... ORDER BY embedding <=> $1 LIMIT $2
- /sync   改成 INSERT INTO memories (session_id, text, embedding) VALUES ...
- 这正是 V7 抽出 ABC 的最终验证点：HTTP 边界让两端独立演化

启动方式：
    pip install fastapi uvicorn
    python scripts/mock_memory_server.py
    # 默认 http://127.0.0.1:8765
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mock_memory")


# ─── 假 embedding ────────────────────────────────────────────────────────────
#
# 真实实现：sentence-transformers / openai embeddings → 384/1536 维 float
# 这里：md5(text) → 16 字节 → 16 个 [0,1] float。
# 性质：同样文本 → 同样向量；不同文本 → 不同向量；可做余弦相似度。
# 局限：完全不懂语义，"猫"和"狗"的相似度可能比"猫"和"猫科动物"还低。
# 教学价值：足以演示"同文本召回"，不需要真正的语义理解。

_VEC_DIM = 16


def fake_embedding(text: str) -> list[float]:
    """md5(text) → 16 维 float（每维 [0, 1]）。"""
    digest = hashlib.md5(text.encode("utf-8")).digest()
    # 每字节 (0-255) 归一化到 [0, 1]
    return [b / 255.0 for b in digest]


def cosine(a: list[float], b: list[float]) -> float:
    """标准余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ─── 进程内存储 ─────────────────────────────────────────────────────────────
#
# 真实实现：Postgres + pgvector（每行：id, session_id, text, embedding, created_at）
# 这里：dict[session_id, list[(text, embedding)]]，进程退出即清空。

_STORE: dict[str, list[tuple[str, list[float]]]] = {}


# ─── 请求/响应模型 ───────────────────────────────────────────────────────────


class RecallRequest(BaseModel):
    query: str
    session_id: str = ""
    k: int = 3


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

app = FastAPI(title="Nano Memory Mock", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """探活端点 — 对应 RemoteSemanticProvider.is_available()。"""
    return {"ok": True, "sessions": len(_STORE), "vec_dim": _VEC_DIM}


@app.post("/recall", response_model=RecallResponse)
def recall(req: RecallRequest) -> RecallResponse:
    """召回与 query 最相似的 k 条记忆 — 对应 prefetch()。

    步骤：query → 假 embedding → 与会话内全部条目算余弦 → 取 top-k。
    """
    bucket = _STORE.get(req.session_id, [])
    if not bucket:
        return RecallResponse(hits=[])

    q_vec = fake_embedding(req.query)
    scored = [(text, cosine(q_vec, vec)) for text, vec in bucket]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[: max(0, req.k)]
    hits = [RecallHit(text=t, score=s) for t, s in top if s > 0]
    logger.info("recall session=%s query=%r → %d hits", req.session_id, req.query, len(hits))
    return RecallResponse(hits=hits)


@app.post("/sync", response_model=SyncResponse)
def sync(req: SyncRequest) -> SyncResponse:
    """持久化一轮对话 — 对应 sync_turn()。

    存储策略：把 user 和 assistant 拼成一条 `User: ... \\nAssistant: ...`
    再算 embedding。真实实现可能拆成两条、做摘要、过滤短回复，这里
    保留最简形态。
    """
    bucket = _STORE.setdefault(req.session_id, [])
    text = f"User: {req.user.strip()}\nAssistant: {req.assistant.strip()}"
    vec = fake_embedding(text)
    bucket.append((text, vec))
    logger.info("sync session=%s entries=%d", req.session_id, len(bucket))
    return SyncResponse(stored=1, total=len(bucket))


# ─── 入口 ────────────────────────────────────────────────────────────────────


def main() -> None:
    host = os.environ.get("MOCK_MEMORY_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_MEMORY_PORT", "8765"))
    logger.info("starting mock memory server on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
