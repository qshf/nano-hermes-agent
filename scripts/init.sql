-- V10.1 — pgvector schema for nano_memory
--
-- 由 docker-compose 在数据库首次初始化时执行（仅当数据卷为空）。
-- 教学项目：手动改完之后只能 `docker compose down -v` 重置，
-- 不引入 migration 工具（alembic）以保持单进程可读。
--
-- ⚠️ 维度必须与 .env 里的 EMBEDDING_DIM 一致 ⚠️
-- 常见模型默认维度（写在这里方便对照修改）：
--   text-embedding-3-small      → 1536（也支持 dimensions 截断到 512/256）
--   text-embedding-3-large      → 3072（也支持截断到 1024/512/256）
--   DashScope text-embedding-v3 → 1024
--   BGE / bge-large-zh-v1.5     → 1024
--   Ollama nomic-embed-text     → 768
--
-- 改维度的步骤：
--   1) 改下方 VECTOR(N) 与 .env 的 EMBEDDING_DIM 同步
--   2) docker compose down -v   # 删卷，让 init.sql 下次重跑
--   3) docker compose up -d
-- mock server 启动时会自检维度，不一致直接 fail-fast。

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL DEFAULT '',
    text        TEXT        NOT NULL,
    embedding   VECTOR(1024) NOT NULL,  -- 与 .env 的 EMBEDDING_DIM 一致
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
