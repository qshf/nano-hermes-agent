-- V10.1 — pgvector schema for nano_memory
--
-- 由 docker-compose 在数据库首次初始化时执行（仅当数据卷为空）。
-- 教学项目：手动改完之后只能 `docker compose down -v` 重置，
-- 不引入 migration 工具（alembic）以保持单进程可读。
--
-- 维度通过 ALTER 时改 — 默认 1536（text-embedding-3-small），
-- 切大模型时 `docker compose down -v` 重建即可。

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL DEFAULT '',
    text        TEXT        NOT NULL,
    embedding   VECTOR(1536) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 按 session 过滤是热路径（recall 总要先按 session_id 过滤）
CREATE INDEX IF NOT EXISTS memories_session_idx
    ON memories (session_id);

-- ivfflat 是 pgvector 推荐的近似最近邻索引；lists=100 适合 <1M 行。
-- 行数大时 lists 应该 ≈ sqrt(rows)，但教学 demo 用固定值最简单。
-- vector_cosine_ops 对应 `<=>` 操作符（cosine distance）。
CREATE INDEX IF NOT EXISTS memories_embedding_idx
    ON memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
