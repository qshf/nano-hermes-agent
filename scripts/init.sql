-- V11 — Knowledge Graph schema for nano_memory (Hindsight 1:1 reproduction)
--
-- 由 docker-compose 在数据库首次初始化时执行（仅当数据卷为空）。
-- 教学项目：手动改完之后只能 `docker compose down -v` 重置。
--
-- ⚠️ VECTOR(N) 维度必须与 .env 里的 EMBEDDING_DIM 一致 ⚠️
-- 常见模型默认维度：
--   text-embedding-3-small      → 1536
--   text-embedding-3-large      → 3072
--   DashScope text-embedding-v3 → 1024
--   BGE / bge-large-zh-v1.5     → 1024
--   Ollama nomic-embed-text     → 768

CREATE EXTENSION IF NOT EXISTS vector;

-- ─── Banks: 命名空间隔离（对应 Hindsight 的 bank 概念）─────────────────────
CREATE TABLE IF NOT EXISTS banks (
    id          BIGSERIAL PRIMARY KEY,
    bank_id     TEXT UNIQUE NOT NULL,
    mission     TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Documents: 逻辑容器（一个 session/对话 对应一个 document）────────────────
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    bank_id     TEXT NOT NULL REFERENCES banks(bank_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    update_mode TEXT NOT NULL DEFAULT 'append',
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bank_id, document_id)
);

-- ─── Entities: 抽取的命名实体（人、项目、工具、偏好等）──────────────────────
CREATE TABLE IF NOT EXISTS entities (
    id          BIGSERIAL PRIMARY KEY,
    bank_id     TEXT NOT NULL REFERENCES banks(bank_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    embedding   VECTOR(1024) NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bank_id, name)
);

-- ─── Relations: 实体间关系（uses, prefers, works_on, knows 等）───────────────
CREATE TABLE IF NOT EXISTS relations (
    id              BIGSERIAL PRIMARY KEY,
    bank_id         TEXT NOT NULL REFERENCES banks(bank_id) ON DELETE CASCADE,
    source_entity   TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    target_entity   TEXT NOT NULL,
    weight          FLOAT NOT NULL DEFAULT 1.0,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bank_id, source_entity, relation_type, target_entity)
);

-- ─── Facts: 原子知识语句，关联实体和文档 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS facts (
    id          BIGSERIAL PRIMARY KEY,
    bank_id     TEXT NOT NULL REFERENCES banks(bank_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL DEFAULT '',
    entity_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    embedding   VECTOR(1024) NOT NULL,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Indexes ────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS entities_bank_idx ON entities(bank_id);
CREATE INDEX IF NOT EXISTS entities_embedding_idx
    ON entities USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS relations_bank_idx ON relations(bank_id);
CREATE INDEX IF NOT EXISTS relations_source_idx ON relations(bank_id, source_entity);
CREATE INDEX IF NOT EXISTS relations_target_idx ON relations(bank_id, target_entity);

CREATE INDEX IF NOT EXISTS facts_bank_idx ON facts(bank_id);
CREATE INDEX IF NOT EXISTS facts_entity_idx ON facts(bank_id, entity_name);
CREATE INDEX IF NOT EXISTS facts_document_idx ON facts(bank_id, document_id);
CREATE INDEX IF NOT EXISTS facts_embedding_idx
    ON facts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS facts_tags_idx ON facts USING gin(tags);

-- ─── Seed default bank ──────────────────────────────────────────────────────

INSERT INTO banks (bank_id, mission)
VALUES ('hermes', 'Default memory bank for nano hermes agent')
ON CONFLICT (bank_id) DO NOTHING;
