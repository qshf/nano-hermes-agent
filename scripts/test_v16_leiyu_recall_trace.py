"""Trace V16 recall internals for Lei Yu ("雷雨") questions.

Unlike scripts/test_v16_leiyu_recall.py, this script does not only call the
/recall endpoint. It replays the same algorithm step by step against the live
database so we can inspect intermediate candidates:

1. direct semantic facts
2. query-matched seed entities
3. N-hop BFS entity expansion
4. facts attached to graph-related entities
5. merged candidate pool
6. final scoring components

Usage:
    .venv/bin/python scripts/test_v16_leiyu_recall_trace.py

Optional:
    .venv/bin/python scripts/test_v16_leiyu_recall_trace.py \
      --query "周朴园和繁漪的冲突体现了什么？" --k 5 --hops 2
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


DEFAULT_QUERY = "周朴园和繁漪的冲突体现了什么？"


def _short(text: str, limit: int = 58) -> str:
    text = text.replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _fmt(value: float) -> str:
    return f"{value:.4f}"


def _make_pool(database_url: str) -> ConnectionPool:
    def configure(conn):
        register_vector(conn)

    pool = ConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=2,
        configure=configure,
        kwargs={"autocommit": True},
    )
    pool.open()
    pool.wait()
    return pool


def _embed(client: OpenAI, model: str, dim: int, text: str) -> list[float]:
    kwargs: dict[str, Any] = {"model": model, "input": text}
    if dim != 1536:
        kwargs["dimensions"] = dim
    resp = client.embeddings.create(**kwargs)
    return resp.data[0].embedding


def _hop_weight(hop_distance: int, hop_decay: float) -> float:
    if hop_distance <= 1:
        return 1.0
    return math.pow(hop_decay, hop_distance - 1)


def _time_weight(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / half_life_days)


def _decay_blend(age_days: float, half_life_days: float, decay_alpha: float) -> float:
    if decay_alpha <= 0:
        return 1.0
    tw = _time_weight(age_days, half_life_days)
    return (1.0 - decay_alpha) + decay_alpha * tw


def _bfs_trace(conn, bank_id: str, seeds: set[str], max_hops: int) -> tuple[dict[str, int], dict[int, list[str]]]:
    visited: dict[str, int] = {name: 1 for name in seeds}
    levels: dict[int, list[str]] = {1: sorted(seeds)}
    if max_hops <= 1:
        return visited, levels

    frontier: set[str] = set(seeds)
    for hop in range(2, max_hops + 1):
        if not frontier:
            levels[hop] = []
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

        levels[hop] = sorted(next_frontier)
        frontier = next_frontier

    return visited, levels


def _print_fact_table(rows: list[dict[str, Any]], include_score: bool = False) -> None:
    if include_score:
        print("| rank | id | score | cosine | source | hop | entity | text |")
        print("|---:|---:|---:|---:|---|---:|---|---|")
        for idx, row in enumerate(rows, 1):
            print(
                f"| {idx} | {row['_id']} | {_fmt(row['score'])} | {_fmt(row['cosine'])} "
                f"| {row['source']} | {row['hop']} | {row['entity_name']} | {_short(row['text'])} |"
            )
        return

    print("| rank | id | cosine | entity | text |")
    print("|---:|---:|---:|---|---|")
    for idx, row in enumerate(rows, 1):
        print(
            f"| {idx} | {row['_id']} | {_fmt(row['cosine'])} "
            f"| {row['entity_name']} | {_short(row['text'])} |"
        )


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Trace V16 recall internals for 雷雨.")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--bank", default=os.environ.get("MEMORY_BANK_ID", "hermes"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--hops", type=int, default=max(1, int(os.environ.get("RECALL_HOPS", "2"))))
    parser.add_argument("--entity-threshold", type=float, default=0.3)
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "postgresql://nano:nano@127.0.0.1:5432/nano_memory")
    embedding_base_url = os.environ.get("EMBEDDING_BASE_URL", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    embedding_api_key = os.environ.get("EMBEDDING_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    embedding_model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_dim = int(os.environ.get("EMBEDDING_DIM", "1024"))
    hop_decay = float(os.environ.get("HOP_DECAY", "0.7"))
    half_life_days = float(os.environ.get("DECAY_HALF_LIFE_DAYS", "30"))
    decay_alpha = float(os.environ.get("DECAY_ALPHA", "0.3"))

    if not embedding_api_key:
        print("EMBEDDING_API_KEY or OPENAI_API_KEY is required", file=sys.stderr)
        return 2

    client = OpenAI(api_key=embedding_api_key, base_url=embedding_base_url)
    pool = _make_pool(database_url)

    try:
        q_vec = _embed(client, embedding_model, embedding_dim, args.query)
        results: dict[int, dict[str, Any]] = {}

        print("# V16 Recall Trace")
        print()
        print(f"- query: `{args.query}`")
        print(f"- bank: `{args.bank}`")
        print(f"- k: `{args.k}`")
        print(f"- hops: `{args.hops}`")
        print(f"- hop_decay: `{hop_decay}`")
        print(f"- half_life_days: `{half_life_days}`")
        print(f"- decay_alpha: `{decay_alpha}`")
        print()

        with pool.connection() as conn:
            print("## 1. 直接语义召回 facts")
            print()
            cur = conn.execute(
                """SELECT id, text, entity_name, updated_at,
                          1 - (embedding <=> %s::vector) AS cosine
                   FROM facts
                   WHERE bank_id = %s
                   ORDER BY embedding <=> %s::vector
                   LIMIT %s;""",
                (q_vec, args.bank, q_vec, args.k * 2),
            )
            semantic_rows: list[dict[str, Any]] = []
            for row in cur.fetchall():
                fid, text, entity_name, updated_at, cosine = row
                item = {
                    "_id": fid,
                    "text": text,
                    "entity_name": entity_name,
                    "updated_at": updated_at,
                    "cosine": float(cosine),
                    "source": "semantic",
                    "hop": 0,
                }
                semantic_rows.append(item)
                results[fid] = item.copy()
            _print_fact_table(semantic_rows)
            print()

            print("## 2. query 语义接近的种子实体")
            print()
            cur = conn.execute(
                """SELECT name, entity_type, 1 - (embedding <=> %s::vector) AS score
                   FROM entities
                   WHERE bank_id = %s
                   ORDER BY embedding <=> %s::vector
                   LIMIT 5;""",
                (q_vec, args.bank, q_vec),
            )
            entity_rows = cur.fetchall()
            seed_entities = {row[0] for row in entity_rows if float(row[2]) > args.entity_threshold}
            print("| rank | entity | type | score | selected |")
            print("|---:|---|---|---:|---|")
            for idx, row in enumerate(entity_rows, 1):
                name, entity_type, score = row
                selected = "yes" if name in seed_entities else "no"
                print(f"| {idx} | {name} | {entity_type} | {_fmt(float(score))} | {selected} |")
            print()

            print("## 3. BFS 多跳图遍历结果")
            print()
            if seed_entities:
                entity_hops, levels = _bfs_trace(conn, args.bank, seed_entities, args.hops)
            else:
                entity_hops, levels = {}, {}
            print("| hop | entities |")
            print("|---:|---|")
            for hop in range(1, args.hops + 1):
                names = levels.get(hop, [])
                print(f"| {hop} | {', '.join(names) if names else '(empty)'} |")
            print()

            print("## 4. 图相关实体下挂的 facts")
            print()
            graph_rows: list[dict[str, Any]] = []
            if entity_hops:
                placeholders = ",".join(["%s"] * len(entity_hops))
                cur = conn.execute(
                    f"""SELECT id, text, entity_name, updated_at,
                               1 - (embedding <=> %s::vector) AS cosine
                        FROM facts
                        WHERE bank_id = %s AND entity_name IN ({placeholders})
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s;""",
                    (q_vec, args.bank, *entity_hops.keys(), q_vec, args.k * args.hops * 2),
                )
                for row in cur.fetchall():
                    fid, text, entity_name, updated_at, cosine = row
                    hop = entity_hops.get(entity_name, 1)
                    graph_rows.append({
                        "_id": fid,
                        "text": text,
                        "entity_name": entity_name,
                        "updated_at": updated_at,
                        "cosine": float(cosine),
                        "source": "graph",
                        "hop": hop,
                    })

                    existing = results.get(fid)
                    if existing is None:
                        results[fid] = graph_rows[-1].copy()
                    elif existing["hop"] == 0 or hop < existing["hop"]:
                        existing["source"] = "graph" if existing["source"] == "semantic" else existing["source"]
                        existing["hop"] = hop

            print("| rank | id | cosine | hop | entity | text |")
            print("|---:|---:|---:|---:|---|---|")
            for idx, row in enumerate(graph_rows, 1):
                print(
                    f"| {idx} | {row['_id']} | {_fmt(row['cosine'])} | {row['hop']} "
                    f"| {row['entity_name']} | {_short(row['text'])} |"
                )
            print()

        print("## 5. 合并后的候选池")
        print()
        merged_rows = sorted(results.values(), key=lambda item: item["cosine"], reverse=True)
        print("| id | source | hop | cosine | entity | text |")
        print("|---:|---|---:|---:|---|---|")
        for row in merged_rows:
            print(
                f"| {row['_id']} | {row['source']} | {row['hop']} | {_fmt(row['cosine'])} "
                f"| {row['entity_name']} | {_short(row['text'])} |"
            )
        print()

        print("## 6. 最终评分拆解")
        print()
        now = datetime.now(timezone.utc)
        scored: list[dict[str, Any]] = []
        for row in results.values():
            ts = row["updated_at"]
            if ts is None:
                age_days = 0.0
            else:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - ts).total_seconds() / 86400.0)

            scoring_hop = row["hop"] if row["hop"] >= 1 else 1
            hw = _hop_weight(scoring_hop, hop_decay)
            tw = _time_weight(age_days, half_life_days)
            db = _decay_blend(age_days, half_life_days, decay_alpha)
            score = row["cosine"] * hw * db
            scored.append({
                **row,
                "score": score,
                "age_days": age_days,
                "hop_weight": hw,
                "time_weight": tw,
                "decay_blend": db,
            })

        scored.sort(key=lambda item: item["score"], reverse=True)
        print("| rank | id | score | cosine | hop_weight | decay_blend | source | hop | text |")
        print("|---:|---:|---:|---:|---:|---:|---|---:|---|")
        for idx, row in enumerate(scored[: args.k], 1):
            print(
                f"| {idx} | {row['_id']} | {_fmt(row['score'])} | {_fmt(row['cosine'])} "
                f"| {_fmt(row['hop_weight'])} | {_fmt(row['decay_blend'])} "
                f"| {row['source']} | {row['hop']} | {_short(row['text'])} |"
            )

        print()
        print("## PASS")
        print("Trace completed.")
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
