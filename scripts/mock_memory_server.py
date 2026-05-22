"""
Mock Memory Server — V16 多跳遍历 + 时间衰减版。

V16 变化（相对 V11）：
- 多跳图遍历：recall 支持 N-hop BFS（默认 RECALL_HOPS=2），跳数越深权重越低
- 时间衰减：fact 按 updated_at 计算 age，半衰期 DECAY_HALF_LIFE_DAYS（默认 30）
- 综合 score = cosine * (hop_weight) * (decay_blend(time_weight))

V11 保留：
- 存储：知识图谱（banks + documents + entities + relations + facts）
- 抽取：LLM 实体/关系/事实三层抽取
- 检索：多策略（语义搜索 + 实体匹配 + 图遍历）
- /reflect（LLM 合成反思）

对应源项目：plugins/memory/hindsight/__init__.py 的服务端逻辑。
"""

from __future__ import annotations

import logging
import math
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


# ─── 配置 ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://nano:nano@127.0.0.1:5432/nano_memory",
)

EMBEDDING_BASE_URL = os.environ.get(
    "EMBEDDING_BASE_URL",
    os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)
EMBEDDING_API_KEY = os.environ.get(
    "EMBEDDING_API_KEY",
    os.environ.get("OPENAI_API_KEY", ""),
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))

LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("MODEL", "deepseek-chat")

# 事实去重阈值 — 同实体下新 fact 与已有 fact 余弦相似度超过此值时 UPDATE
FACT_DEDUP_THRESHOLD = float(os.environ.get("FACT_DEDUP_THRESHOLD", "0.92"))

# V16: 多跳图遍历配置
# RECALL_HOPS=1 退回 V11 行为；2 表示 1-hop + 2-hop 邻居都参与召回
RECALL_HOPS = max(1, int(os.environ.get("RECALL_HOPS", "2")))
# 跨跳衰减系数 — 第 N 跳的实体上 fact 权重 = HOP_DECAY ** N
HOP_DECAY = float(os.environ.get("HOP_DECAY", "0.7"))

# V16: 时间衰减配置
# 半衰期：每过 N 天 time_weight 衰减一半（指数衰减）；<=0 关闭衰减
DECAY_HALF_LIFE_DAYS = float(os.environ.get("DECAY_HALF_LIFE_DAYS", "30"))
# decay 在最终 score 中的权重；其余为纯 cosine
# final = cosine * ((1 - alpha) + alpha * time_weight)
DECAY_ALPHA = float(os.environ.get("DECAY_ALPHA", "0.3"))


# ─── 全局资源 ────────────────────────────────────────────────────────────────

_pool: ConnectionPool | None = None
_embedding_client: OpenAI | None = None
_llm_client: OpenAI | None = None


def _make_pool() -> ConnectionPool:
    def _configure(conn):
        register_vector(conn)

    return ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=5,
        configure=_configure,
        kwargs={"autocommit": True},
    )


def _probe_embedding_dim(table: str, column: str = "embedding") -> int | None:
    """读取指定表的 vector 列维度。"""
    if _pool is None:
        return None
    try:
        with _pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = %s AND a.attname = %s;
                """,
                (table, column),
            )
            row = cur.fetchone()
            if not row:
                return None
            return int(row[0])
    except Exception as e:
        logger.warning("probe embedding dim failed: %s", e)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建池 + 校验维度，关闭时释放。"""
    global _pool, _embedding_client, _llm_client
    logger.info("connecting to db: %s", DATABASE_URL.split("@")[-1])
    _pool = _make_pool()
    _pool.open()
    _pool.wait()

    # 维度自检
    for table in ("entities", "facts"):
        db_dim = _probe_embedding_dim(table)
        if db_dim is not None and db_dim != EMBEDDING_DIM:
            raise RuntimeError(
                f"Embedding dim mismatch: {table}.embedding is VECTOR({db_dim}) but "
                f"EMBEDDING_DIM={EMBEDDING_DIM}. Run:\n"
                f"  docker compose down -v\n"
                f"  # edit scripts/init.sql VECTOR({EMBEDDING_DIM})\n"
                f"  docker compose up -d"
            )

    _embedding_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)
    logger.info("embedding: %s (model=%s, dim=%d)", EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM)

    _llm_client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    logger.info("llm: %s (model=%s)", LLM_BASE_URL, LLM_MODEL)
    logger.info(
        "v16: recall_hops=%d, hop_decay=%.2f, decay_half_life=%.1fd, decay_alpha=%.2f",
        RECALL_HOPS, HOP_DECAY, DECAY_HALF_LIFE_DAYS, DECAY_ALPHA,
    )

    yield

    logger.info("shutting down")
    if _pool is not None:
        _pool.close()


# ─── Embedding ───────────────────────────────────────────────────────────────


def embed(text: str) -> list[float]:
    """单条 embedding（OpenAI 兼容范式）。"""
    if _embedding_client is None:
        raise RuntimeError("embedding client not initialized")
    kwargs: dict[str, Any] = {"model": EMBEDDING_MODEL, "input": text}
    if EMBEDDING_DIM != 1536:
        kwargs["dimensions"] = EMBEDDING_DIM
    resp = _embedding_client.embeddings.create(**kwargs)
    return resp.data[0].embedding


# ─── LLM 知识图谱抽取（Hindsight 核心模式）────────────────────────────────────
#
# 源项目 Hindsight 在 retain 时做：
#   1. 实体识别（人名、项目名、工具、偏好）
#   2. 关系抽取（"用户 uses Python"、"项目 depends_on Redis"）
#   3. 事实抽取（原子知识语句，关联到主实体）
#
# nano 用一次 LLM 调用同时抽取三层结构。

_EXTRACT_KG_SYSTEM_PROMPT = """你是一个知识图谱抽取器。从对话中提取实体、关系和事实。

输出格式（严格遵守，每个区块用标题行开头）：

ENTITIES:
实体名 | 实体类型
（类型可选：person, project, tool, preference, location, organization, concept）

RELATIONS:
源实体 | 关系类型 | 目标实体
（关系类型用简单动词：uses, prefers, works_on, knows, lives_in, depends_on, is_a, has）

FACTS:
关联实体 | 事实内容
（每条事实独立成句，简洁明确，不超过 60 字）

规则：
1. 实体名用规范形式（如 "Python" 不是 "python 语言"）
2. 只提取有长期价值的信息（偏好、个人信息、项目约定、技术栈、重要决策）
3. 忽略寒暄、临时任务状态
4. 如果对话没有值得提取的内容，所有区块留空
5. 每条用 | 分隔，不加编号

示例输入：
User: 我是小明，我在用 Python 做一个叫 hermes 的 AI Agent 项目，部署在 AWS 上
Assistant: 好的！hermes 项目听起来很有趣。

示例输出：
ENTITIES:
小明 | person
Python | tool
hermes | project
AWS | tool

RELATIONS:
小明 | works_on | hermes
hermes | uses | Python
hermes | deployed_on | AWS

FACTS:
小明 | 用户名字是小明
hermes | hermes 是一个 AI Agent 项目
hermes | hermes 使用 Python 开发
hermes | hermes 部署在 AWS 上"""


def extract_knowledge_graph(content: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]], list[tuple[str, str]]]:
    """调用 LLM 从对话中抽取实体、关系、事实。

    返回: (entities[(name, type)], relations[(src, rel, tgt)], facts[(entity, text)])
    """
    if _llm_client is None:
        return [], [], []

    try:
        resp = _llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _EXTRACT_KG_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            temperature=0.0,
            max_tokens=1500,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return [], [], []
        return _parse_kg_output(raw)
    except Exception as e:
        logger.warning("knowledge graph extraction failed: %s", e)
        return [], [], []


def _parse_kg_output(raw: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]], list[tuple[str, str]]]:
    """解析 LLM 输出的结构化知识图谱。"""
    entities: list[tuple[str, str]] = []
    relations: list[tuple[str, str, str]] = []
    facts: list[tuple[str, str]] = []

    current_section = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("ENTITIES"):
            current_section = "entities"
            continue
        elif line.upper().startswith("RELATIONS"):
            current_section = "relations"
            continue
        elif line.upper().startswith("FACTS"):
            current_section = "facts"
            continue

        parts = [p.strip() for p in line.split("|")]

        if current_section == "entities" and len(parts) >= 2:
            entities.append((parts[0], parts[1]))
        elif current_section == "relations" and len(parts) >= 3:
            relations.append((parts[0], parts[1], parts[2]))
        elif current_section == "facts" and len(parts) >= 2:
            facts.append((parts[0], parts[1]))

    logger.info("extracted: %d entities, %d relations, %d facts", len(entities), len(relations), len(facts))
    return entities, relations, facts


# ─── 知识图谱存储操作 ────────────────────────────────────────────────────────


def ensure_bank(bank_id: str) -> None:
    """确保 bank 存在（不存在则创建）。"""
    with _pool.connection() as conn:
        conn.execute(
            "INSERT INTO banks (bank_id) VALUES (%s) ON CONFLICT (bank_id) DO NOTHING;",
            (bank_id,),
        )


def ensure_document(bank_id: str, document_id: str) -> None:
    """确保 document 存在。"""
    if not document_id:
        return
    with _pool.connection() as conn:
        conn.execute(
            """INSERT INTO documents (bank_id, document_id)
               VALUES (%s, %s)
               ON CONFLICT (bank_id, document_id) DO UPDATE SET updated_at = now();""",
            (bank_id, document_id),
        )


def merge_entity(bank_id: str, name: str, entity_type: str) -> None:
    """UPSERT 实体 — 同名实体合并（Hindsight 的实体级去重）。"""
    try:
        vec = embed(name)
    except Exception as e:
        logger.warning("embed entity %r failed: %s", name, e)
        return

    with _pool.connection() as conn:
        # 实体按 (bank_id, name) 唯一：第一次插入，已存在则刷新类型/向量和更新时间。
        conn.execute(
            """INSERT INTO entities (bank_id, name, entity_type, embedding)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (bank_id, name) DO UPDATE SET
                   entity_type = EXCLUDED.entity_type,
                   embedding = EXCLUDED.embedding,
                   updated_at = now();""",
            (bank_id, name, entity_type, vec),
        )


def merge_relation(bank_id: str, source: str, relation_type: str, target: str) -> None:
    """UPSERT 关系 — 同三元组累加 weight。"""
    with _pool.connection() as conn:
        # 同一条关系重复出现时不插入新行，而是把已有关系的权重加 1。
        conn.execute(
            """INSERT INTO relations (bank_id, source_entity, relation_type, target_entity)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (bank_id, source_entity, relation_type, target_entity)
               DO UPDATE SET weight = relations.weight + 1;""",
            (bank_id, source, relation_type, target),
        )


def store_fact(bank_id: str, document_id: str, entity_name: str,
               text: str, tags: list[str]) -> None:
    """存储 fact — 同实体下高相似度时 UPDATE（事实覆盖）。"""
    try:
        vec = embed(text)
    except Exception as e:
        logger.warning("embed fact failed: %s", e)
        return

    with _pool.connection() as conn:
        # 同实体下查找最相似的已有 fact
        cur = conn.execute(
            """SELECT id, text, 1 - (embedding <=> %s::vector) AS score
               FROM facts
               WHERE bank_id = %s AND entity_name = %s
               ORDER BY embedding <=> %s::vector
               LIMIT 1;""",
            (vec, bank_id, entity_name, vec),
        )
        row = cur.fetchone()

        if row and float(row[2]) >= FACT_DEDUP_THRESHOLD:
            conn.execute(
                """UPDATE facts SET text = %s, embedding = %s, document_id = %s,
                          tags = %s, updated_at = now()
                   WHERE id = %s;""",
                (text, vec, document_id, tags, row[0]),
            )
            logger.info("fact UPDATE: %r → %r (score=%.3f)", row[1][:30], text[:30], float(row[2]))
        else:
            conn.execute(
                """INSERT INTO facts (bank_id, document_id, entity_name, text, embedding, tags)
                   VALUES (%s, %s, %s, %s, %s, %s);""",
                (bank_id, document_id, entity_name, text, vec, tags),
            )


# ─── 多策略检索（Hindsight recall 核心模式）──────────────────────────────────
#
# V16 把源项目 Hindsight 的两个生产特性纳入 nano 版：
#   1. 多跳图遍历（BFS）：从 query-matched 实体出发逐跳扩展，跳数越深 fact 权重越低。
#      源项目通过独立 `hindsight_embed` 库做 graph rerank，nano 在 SQL 层用迭代 BFS
#      复现核心思想。
#   2. 时间衰减：用 fact.updated_at 计算 age，按指数半衰期降权，让旧记忆自然褪色，
#      避免过期信息覆盖近期事实。
#
# 综合公式：
#   final_score = base_cosine * hop_weight(d) * decay_blend(age)
#   hop_weight(d)     = HOP_DECAY ** (d - 1)             # d=1 不打折
#   decay_blend(age)  = (1 - DECAY_ALPHA) + DECAY_ALPHA * 2^(-age_days / half_life)


def _time_weight(age_days: float) -> float:
    """指数半衰期：age=0 → 1.0；age=half_life → 0.5。half_life<=0 → 1.0（关闭衰减）。"""
    if DECAY_HALF_LIFE_DAYS <= 0:
        return 1.0
    return math.pow(0.5, age_days / DECAY_HALF_LIFE_DAYS)


def _decay_blend(age_days: float) -> float:
    """混合系数：DECAY_ALPHA=0 → 1.0（关闭）；=1 → 完全跟随 time_weight。"""
    if DECAY_ALPHA <= 0:
        return 1.0
    tw = _time_weight(age_days)
    return (1.0 - DECAY_ALPHA) + DECAY_ALPHA * tw


def _hop_weight(hop_distance: int) -> float:
    """跳数权重：hop=1 → 1.0；hop=2 → HOP_DECAY；以此类推。"""
    if hop_distance <= 1:
        return 1.0
    return math.pow(HOP_DECAY, hop_distance - 1)


def _bfs_neighbors(conn, bank_id: str, seeds: set[str], max_hops: int) -> dict[str, int]:
    """从 seeds 出发逐跳 BFS，返回 {entity_name: hop_distance}（distance 取最小值）。

    seeds 中的实体距离记为 1（直接命中），下一跳为 2，再下一跳 3。
    每跳查 relations 表的双向邻居（source/target 互通）。
    """
    visited: dict[str, int] = {name: 1 for name in seeds}
    if max_hops <= 1:
        return visited
    frontier: set[str] = set(seeds)
    for hop in range(2, max_hops + 1):
        if not frontier:
            break
        placeholders = ",".join(["%s"] * len(frontier))
        cur = conn.execute(
            f"""SELECT DISTINCT target_entity AS neighbor FROM relations
                WHERE bank_id = %s AND source_entity IN ({placeholders})
                UNION
                SELECT DISTINCT source_entity AS neighbor FROM relations
                WHERE bank_id = %s AND target_entity IN ({placeholders});""",
            (bank_id, *frontier, bank_id, *frontier),
        )
        next_frontier: set[str] = set()
        for row in cur.fetchall():
            name = row[0]
            if name in visited:
                continue
            visited[name] = hop
            next_frontier.add(name)
        frontier = next_frontier
    return visited


def recall_multi_strategy(
    bank_id: str,
    query: str,
    k: int,
    tags: list[str] | None = None,
    tags_match: str = "any",
    hops: int | None = None,
) -> list[dict[str, Any]]:
    """多策略检索 — 语义 + 实体匹配 + N-hop 图遍历 + 时间衰减。

    hops=None → 用 RECALL_HOPS 默认值；hops=1 退回 V11 单跳行为。
    返回每条 hit 含 score（最终）、cosine（原始相似度）、source（语义/图）、
    hop（图遍历跳数）、age_days（年龄）、time_weight（衰减系数）。
    """
    try:
        q_vec = embed(query)
    except Exception as e:
        logger.warning("embed query failed: %s", e)
        return []

    n_hops = RECALL_HOPS if hops is None else max(1, int(hops))
    results: dict[int, dict[str, Any]] = {}

    with _pool.connection() as conn:
        # Strategy 1: 语义搜索 facts（source='semantic', hop=0 表示非图召回）
        tag_filter = ""
        params: list[Any] = [q_vec, bank_id, q_vec, k * 2]
        if tags:
            if tags_match == "all":
                tag_filter = "AND tags @> %s"
            else:
                tag_filter = "AND tags && %s"
            params = [q_vec, bank_id, tags, q_vec, k * 2]

        sql = f"""
            SELECT id, text, entity_name, updated_at,
                   1 - (embedding <=> %s::vector) AS cosine
            FROM facts
            WHERE bank_id = %s {tag_filter}
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
        """
        cur = conn.execute(sql, params)
        rows = cur.fetchall()

        for row in rows:
            fid, text, entity_name, updated_at, cosine = row
            cosine = float(cosine)
            results[fid] = {
                "_id": fid,
                "text": text,
                "entity_name": entity_name,
                "updated_at": updated_at,
                "cosine": cosine,
                "source": "semantic",
                "hop": 0,
            }

        # Strategy 2: 实体匹配 — 找语义上接近 query 的实体
        cur = conn.execute(
            """SELECT name, 1 - (embedding <=> %s::vector) AS score
               FROM entities
               WHERE bank_id = %s
               ORDER BY embedding <=> %s::vector
               LIMIT 5;""",
            (q_vec, bank_id, q_vec),
        )
        seed_entities: set[str] = {row[0] for row in cur.fetchall() if float(row[1]) > 0.3}

        # Strategy 3: N-hop BFS 图遍历 — 收集每个邻居实体的最短跳数
        if seed_entities:
            entity_hops = _bfs_neighbors(conn, bank_id, seed_entities, n_hops)
        else:
            entity_hops = {}

        if entity_hops:
            placeholders = ",".join(["%s"] * len(entity_hops))
            cur = conn.execute(
                f"""SELECT id, text, entity_name, updated_at,
                           1 - (embedding <=> %s::vector) AS cosine
                    FROM facts
                    WHERE bank_id = %s AND entity_name IN ({placeholders})
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s;""",
                (q_vec, bank_id, *entity_hops.keys(), q_vec, k * n_hops * 2),
            )
            for row in cur.fetchall():
                fid, text, entity_name, updated_at, cosine = row
                cosine = float(cosine)
                hop = entity_hops.get(entity_name, 1)
                existing = results.get(fid)
                # 同一 fact 已在语义召回中：保留更优的 hop（语义本身记 hop=0，
                # 但图遍历命中说明它也是邻居，仍按图算分能享受 1.0 hop_weight）
                if existing is None:
                    results[fid] = {
                        "_id": fid,
                        "text": text,
                        "entity_name": entity_name,
                        "updated_at": updated_at,
                        "cosine": cosine,
                        "source": "graph",
                        "hop": hop,
                    }
                else:
                    # 图遍历命中已有 fact：若 hop 更近（=更高 hop_weight），更新 source
                    if existing["hop"] == 0 or hop < existing["hop"]:
                        existing["source"] = "graph" if existing["source"] == "semantic" else existing["source"]
                        existing["hop"] = hop

    # ─── 综合打分（cosine × hop_weight × decay_blend）────────────────────
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    scored: list[dict[str, Any]] = []
    for r in results.values():
        cosine = r["cosine"]
        hop = r["hop"] if r["hop"] >= 1 else 1  # 语义直接命中按 1 跳算分（不打折）
        ts = r["updated_at"]
        if ts is None:
            age_days = 0.0
        else:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        hw = _hop_weight(hop)
        db = _decay_blend(age_days)
        final_score = cosine * hw * db
        scored.append({
            "text": r["text"],
            "score": final_score,
            "cosine": cosine,
            "source": r["source"],
            "hop": r["hop"],
            "age_days": round(age_days, 2),
            "time_weight": round(_time_weight(age_days), 4),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:k]
    logger.info(
        "recall bank=%s query=%r hops=%d → %d/%d (hop_decay=%.2f, half_life=%.1fd)",
        bank_id, query[:40], n_hops, len(top), len(scored), HOP_DECAY, DECAY_HALF_LIFE_DAYS,
    )
    return top


# ─── Reflect 合成（Hindsight reflect 核心模式）───────────────────────────────
#
# 源项目 Hindsight 的 reflect 端点：
#   1. 先 recall 获取相关记忆
#   2. 把记忆作为上下文，让 LLM 合成一个连贯的回答
#   3. 返回单一文本（不是 hit 列表）

_REFLECT_SYSTEM_PROMPT = """你是一个记忆合成器。根据提供的记忆片段，对用户的问题给出连贯、准确的回答。

规则：
1. 只使用提供的记忆内容回答，不要编造
2. 如果记忆不足以回答，明确说明
3. 综合多条记忆给出完整回答
4. 保持简洁，不超过 200 字"""


def reflect_synthesize(bank_id: str, query: str, k: int) -> str:
    """检索相关记忆后让 LLM 合成回答。"""
    results = recall_multi_strategy(bank_id, query, k * 2)
    if not results:
        return "没有找到相关记忆。"

    context = "\n".join(f"- {r['text']}" for r in results)

    if _llm_client is None:
        return context

    try:
        resp = _llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _REFLECT_SYSTEM_PROMPT},
                {"role": "user", "content": f"记忆片段：\n{context}\n\n问题：{query}"},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("reflect synthesis failed: %s", e)
        return context


# ─── 请求/响应模型 ───────────────────────────────────────────────────────────

_BUDGET_TO_K = {"low": 2, "mid": 5, "high": 10}


class RetainRequest(BaseModel):
    bank_id: str = "hermes"
    content: str
    document_id: str = ""
    context: str = ""
    metadata: dict[str, str] = {}
    tags: list[str] = []
    update_mode: str = "append"


class RetainResponse(BaseModel):
    ok: bool
    entities_extracted: int
    relations_extracted: int
    facts_stored: int


class RecallRequest(BaseModel):
    bank_id: str = "hermes"
    query: str
    budget: str = "mid"
    tags: list[str] | None = None
    tags_match: str = "any"


class RecallResult(BaseModel):
    text: str
    score: float
    source: str = ""
    cosine: float = 0.0
    hop: int = 0
    age_days: float = 0.0
    time_weight: float = 1.0


class RecallResponse(BaseModel):
    results: list[RecallResult]


class ReflectRequest(BaseModel):
    bank_id: str = "hermes"
    query: str
    budget: str = "mid"


class ReflectResponse(BaseModel):
    text: str


# ─── 应用 ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Nano Memory v16 (Multi-hop + Decay)", version="0.4.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """探活 + 资源状态。"""
    db_ok = False
    stats: dict[str, int] = {}
    try:
        if _pool is not None:
            with _pool.connection() as conn:
                for table in ("banks", "entities", "relations", "facts"):
                    cur = conn.execute(f"SELECT count(*) FROM {table};")
                    stats[table] = cur.fetchone()[0]
            db_ok = True
    except Exception as e:
        logger.warning("healthz db probe failed: %s", e)

    return {
        "ok": db_ok,
        "db_connected": db_ok,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
        **stats,
    }


@app.post("/retain", response_model=RetainResponse)
def retain(req: RetainRequest) -> RetainResponse:
    """持久化对话 — 服务端做实体/关系/事实抽取（Hindsight retain 模式）。

    客户端发送原始对话内容，服务端负责：
    1. LLM 抽取 entities + relations + facts
    2. 实体级去重（UPSERT by name）
    3. 关系累加（同三元组 weight+1）
    4. 事实去重（同实体+高相似度 → UPDATE）
    """
    if _pool is None:
        raise HTTPException(500, "db pool not initialized")

    ensure_bank(req.bank_id)
    if req.document_id:
        ensure_document(req.bank_id, req.document_id)

    entities, relations, facts = extract_knowledge_graph(req.content)

    if not entities and not relations and not facts:
        return RetainResponse(ok=True, entities_extracted=0, relations_extracted=0, facts_stored=0)

    for name, etype in entities:
        merge_entity(req.bank_id, name, etype)

    for src, rel, tgt in relations:
        merge_relation(req.bank_id, src, rel, tgt)

    for entity_name, fact_text in facts:
        store_fact(req.bank_id, req.document_id, entity_name, fact_text, req.tags)

    return RetainResponse(
        ok=True,
        entities_extracted=len(entities),
        relations_extracted=len(relations),
        facts_stored=len(facts),
    )


@app.post("/recall", response_model=RecallResponse)
def recall(req: RecallRequest) -> RecallResponse:
    """多策略语义检索 — 语义搜索 + 实体匹配 + 图遍历。"""
    if _pool is None:
        raise HTTPException(500, "db pool not initialized")

    k = _BUDGET_TO_K.get(req.budget.lower(), 5)
    results = recall_multi_strategy(req.bank_id, req.query, k, req.tags, req.tags_match)
    return RecallResponse(results=[RecallResult(**r) for r in results])


@app.post("/reflect", response_model=ReflectResponse)
def reflect(req: ReflectRequest) -> ReflectResponse:
    """LLM 合成反思 — 检索记忆后合成连贯回答。"""
    if _pool is None:
        raise HTTPException(500, "db pool not initialized")

    k = _BUDGET_TO_K.get(req.budget.lower(), 5)
    text = reflect_synthesize(req.bank_id, req.query, k)
    return ReflectResponse(text=text)



# ─── 入口 ────────────────────────────────────────────────────────────────────


def main() -> None:
    host = os.environ.get("MOCK_MEMORY_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_MEMORY_PORT", "8765"))
    logger.info("starting mock memory server on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
