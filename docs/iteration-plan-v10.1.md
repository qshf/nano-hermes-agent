# V10.1 迭代规划 — pgvector + OpenAI Embedding

## 一句话

V10 的 mock server 用 `dict[session_id, list[(text, md5_vec)]]` 演示了"钩子→HTTP 端点"的形状；V10.1 把存储换成 **PostgreSQL + pgvector**、向量换成 **OpenAI embeddings**，并让 Provider 端按 Hindsight 路线同步演进出 `budget` 和 `min_score` 配置 — 验证 V7 ABC 抽象的"两端独立演化"在真实数据库场景下成立。

## 上一版本（V10）暴露的问题

| 问题 | 表现 |
|------|------|
| 假向量没有语义 | `md5("猫")` 与 `md5("猫科动物")` 的余弦距离纯随机，召回返回的"top-k"对模型毫无价值 |
| 进程内 dict 一退出就丢 | mock server 重启后所有 `sync` 记录消失，无法演示跨会话长期记忆 |
| 无召回质量阈值 | 即使最相似的 hit 也只是 0.3，全都被返回 — 模型读了一堆噪声 |
| 召回数量写死 `top_k=3` | 不像 Hindsight 的 `budget=low/mid/high` 那样能在"召回广度 vs token 预算"之间权衡 |

## 引入的核心概念

### 1. pgvector 向量列 + 余弦操作符
```sql
CREATE EXTENSION vector;
embedding VECTOR(1536)
ORDER BY embedding <=> $1   -- <=> 是 cosine distance
```
把"算余弦相似度 + 排序 + top-k"从 Python 列表推导式下推到数据库，迁移到真后端时 SQL 一行不改。

### 2. OpenAI Embedding（范式）
配置从 `.env` 加载：
- `EMBEDDING_BASE_URL`（默认与 `OPENAI_BASE_URL` 同源）
- `EMBEDDING_API_KEY`
- `EMBEDDING_MODEL`（默认 `text-embedding-3-small`）
- `EMBEDDING_DIM`（默认 1536）

任何 OpenAI 兼容端点（OpenAI、DeepSeek、SiliconFlow、本地 vLLM/llama.cpp）都能直接接入。

### 3. Hindsight 路线的 budget / min_score
| Provider 端 | 含义 | 服务端行为 |
|-------------|------|----------|
| `budget=low/mid/high` | 召回广度档位 | low→k=2，mid→k=5，high→k=10 |
| `min_score=0.3` | 相似度阈值 | 低于该值的 hit 在 SQL 层就过滤掉 |

对应源项目 `plugins/memory/hindsight/__init__.py:1310-1323` 的 `recall_kwargs` 构造。

### 4. docker-compose 一键起 pgvector
`pgvector/pgvector:pg16` 镜像自带扩展；`scripts/init.sql` 通过 `/docker-entrypoint-initdb.d/` 在首次启动时自动建表 + 建索引。读者只要 `docker compose up -d` 就有一个干净的 pg 在跑。

## 不变的部分（重点：架构验证）

| 维度 | V10 | V10.1 |
|------|-----|-------|
| Provider HTTP 边界 | ✓ | ✓（一行不改） |
| `MemoryProvider` ABC | ✓ | ✓ |
| `MemoryManager` 编排 | ✓ | ✓ |
| 生命周期钩子（prefetch / sync_turn） | ✓ | ✓ |
| 围栏（sanitize + memory-context） | ✓ | ✓ |
| 端点签名（/healthz, /recall, /sync） | ✓ | ✓（请求体加了 `budget` `min_score`，旧字段全兼容） |

**架构验证点**：V7 抽出 ABC 时承诺的"把 mock server 换成真后端，Provider 端不改"，在 V10.1 兑现 —— `RemoteSemanticProvider` 的 HTTP 调用代码 0 改动（只新增配置项，不改请求时序）。

## 暴露的新问题（引出 V10.2+ 的可能方向）

| 痛点 | 可能解法（不在本版本范围） |
|------|-----------------------|
| 每条 recall 都同步阻塞调用 embedding API（往返 + 余额消耗） | 客户端缓存 + 批量 embedding + 异步预热下一轮 |
| sync_turn 同步阻塞影响主循环延迟 | 写入走后台线程 + 队列（源项目 Hindsight 的 `aretain_batch` 模式） |
| 没有去重 / 实体抽取 / 摘要 | LLM 中间层做事实抽取（Hindsight 的 `retain` 里做） |
| 没有跨 session 的全局视图 | 让 `bank_id` 与 `session_id` 解耦 |

## 对应源项目文件

| 本版本变更点 | 源项目对照 |
|-------------|----------|
| pgvector schema | `hindsight-embed` 内置 Postgres |
| OpenAI 范式 embedding | `plugins/memory/hindsight/__init__.py:401-438` `_build_embedded_profile_env` 里的 OpenAI 兼容配置 |
| budget / min_score | `plugins/memory/hindsight/__init__.py:1310-1323` recall_kwargs |
| HTTP 边界 | `plugins/memory/hindsight/` 的 `local_external` 模式 |
| docker-compose | Hindsight Docker 自托管路径 |

## 验证清单

- [ ] `docker compose up -d` 起来后 `psql` 能看到 `memories` 表和 `vector` 扩展
- [ ] `python scripts/mock_memory_server.py` 启动，`/healthz` 返回 200 且报告 db_connected
- [ ] `python agent.py`（设 `MEMORY_SERVICE_URL`）能在 banner 看到 `remote_semantic` provider
- [ ] 多轮对话后 `psql` 查 `SELECT count(*) FROM memories;` 数量增长
- [ ] 询问相关问题时，`/sync` 历史能被召回（不再是假向量随机命中）
- [ ] Provider 端 `git diff v10 v10.1 -- memory/remote_semantic.py` 只是新增配置项，无请求时序改动
