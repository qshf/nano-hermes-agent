# nano_hermes_agent — 项目上下文 primer

> **新会话起手必读。** 本文件是 `nano-project-builder` skill 的"交付物零"，
> 用来在跨目录、跨会话的 LLM 协作中保持上下文一致。每完成一个版本必须更新。

---

## 1. TL;DR

- **项目定位**：教学版 AI Agent，从零迭代演进到能挂载长期记忆。
- **源项目**：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级 AI Agent，含 gateway / 多模型后端 / SQLite 会话 / 多终端环境 / 插件系统）。
- **当前阶段**：v11 已完成 — 知识图谱记忆（Hindsight 1:1 复现）。服务端做实体/关系/事实抽取，多策略检索（语义+图遍历），LLM reflect 合成，Provider 支持 context/tools/hybrid 三种模式。
- **核心叙事**：通过 V0→V11 的 12 档迭代，每一档解决前一档暴露的具体痛点，最终从扁平向量存储演进到完整知识图谱。

---

## 2. 路径与仓库

| 角色 | 绝对路径 | git remote | 主分支 |
|------|---------|-----------|-------|
| **源项目** | `/Users/qshf/my-project/hermes-agent` | `https://github.com/qshf/hermes-agent` | `main` |
| **nano 项目** | `/Users/qshf/my-project/nano_hermes_agent` | `git@github.com:qshf/nano-hermes-agent.git` | 多分支 `v0`..`v10.1`，无 main |
| **当前活跃分支** | `v10.1`（HEAD: `ca50210`） | — | — |

**跨目录的硬约束**：源项目和 nano 不在同一目录。任何"对照源项目读 X 文件"的操作都必须用源项目的绝对路径，例：
- 源项目 Hindsight 插件：`/Users/qshf/my-project/hermes-agent/plugins/memory/hindsight/__init__.py`
- 源项目记忆迭代规划：`/Users/qshf/my-project/hermes-agent/docs/memory-system/iteration-plan.md`

---

## 3. 进度状态（12 档迭代）

| 版本 | 标题 | 引入概念 | 状态 |
|------|------|---------|------|
| v0 | if/elif 工具调度 | 字符串硬编码 | ✅ |
| v1 | 工具注册表 | dict 注册 + 元数据 | ✅ |
| v2 | OpenAI 兼容 tool calling | function/tool schema | ✅ |
| v3 | 工具分组（toolsets） | 按场景挂载 | ✅ |
| v4 | system prompt 构建器 | prompt 拼装 | ✅ |
| v5 | MCP 协议接入 | 跨进程工具 | ✅ |
| v6 | 文件记忆（builtin） | MemoryStore + 冻结快照 | ✅ |
| v7 | MemoryProvider ABC | 抽象后端接口 | ✅ |
| v8 | MemoryManager 编排 | 路由 + 围栏 + 隔离 | ✅ |
| v9 | Agent Loop 生命周期 | prefetch / sync_turn 钩子 | ✅ |
| v10 | RemoteSemanticProvider | HTTP 边界（mock dict） | ✅ |
| v10.1 | pgvector + OpenAI embedding | 真实向量存储 + 范式 embedding | ✅ |
| **v11** | **知识图谱记忆（Hindsight 1:1）** | **实体/关系/事实抽取 + 多策略检索 + reflect 合成 + memory mode** | **✅ 已完成** |

**下一档候选**（未启动）：v12 异步 retain（后台线程 + 队列）。

---

## 4. 环境前置

### 4.1 必填 env（agent 主进程）
```bash
OPENAI_API_KEY=...           # 对话模型 key（DeepSeek/OpenAI/...）
OPENAI_BASE_URL=...          # 对话端点
MODEL=deepseek-chat          # 模型名
```

### 4.2 v11 新增（mock server 端）
```bash
DATABASE_URL=postgresql://nano:nano@127.0.0.1:5432/nano_memory
EMBEDDING_API_KEY=...        # 默认 fallback 到 OPENAI_API_KEY
EMBEDDING_BASE_URL=...       # 默认 fallback 到 OPENAI_BASE_URL
EMBEDDING_MODEL=text-embedding-v3   # 当前实测：DashScope
EMBEDDING_DIM=1024            # ⚠️ 必须与 init.sql 的 VECTOR(N) 一致
```

### 4.3 外部依赖
- **Postgres + pgvector**：通过 `docker-compose.yml` 起 `pgvector/pgvector:pg16`（容器名 `nano-memory-pg`，端口 `127.0.0.1:5432`）。
- **mock memory server**：`scripts/mock_memory_server.py`（FastAPI + 知识图谱），监听 `127.0.0.1:8765`。

### 4.4 启用远端记忆（agent 端）
```bash
MEMORY_SERVICE_URL=http://127.0.0.1:8765   # 不设则只挂 builtin provider
MEMORY_SESSION_ID=default
MEMORY_MODE=hybrid                          # context / tools / hybrid
MEMORY_BANK_ID=hermes                       # bank 命名空间
MEMORY_PREFETCH_METHOD=recall               # recall / reflect
```

---

## 5. 验活 cheatsheet

```bash
# 当前在哪个分支
git -C /Users/qshf/my-project/nano_hermes_agent branch --show-current

# DB 容器健康吗
docker ps --filter name=nano-memory-pg --format "table {{.Names}}\t{{.Status}}"

# DB 知识图谱表状态
docker exec nano-memory-pg psql -U nano -d nano_memory -c "SELECT 'entities' as t, count(*) FROM entities UNION ALL SELECT 'relations', count(*) FROM relations UNION ALL SELECT 'facts', count(*) FROM facts;"

# mock server 起没起、健不健康
curl -s --noproxy '*' http://127.0.0.1:8765/healthz | python -m json.tool

# 测试 /retain
curl -s --noproxy '*' -X POST http://127.0.0.1:8765/retain -H 'Content-Type: application/json' -d '{"content":"User: 我叫小明，在用Python做hermes项目\nAssistant: 好的！"}' | python -m json.tool

# 测试 /recall
curl -s --noproxy '*' -X POST http://127.0.0.1:8765/recall -H 'Content-Type: application/json' -d '{"query":"小明用什么语言"}' | python -m json.tool

# 测试 /reflect
curl -s --noproxy '*' -X POST http://127.0.0.1:8765/reflect -H 'Content-Type: application/json' -d '{"query":"告诉我关于用户的所有信息"}' | python -m json.tool

# 重建 DB（schema 变了必须重建）
cd /Users/qshf/my-project/nano_hermes_agent && docker compose down -v && docker compose up -d

# 起 mock server（前台日志，调试用）
cd /Users/qshf/my-project/nano_hermes_agent && \
  .venv/bin/python -m dotenv -f .env run -- .venv/bin/python scripts/mock_memory_server.py
```

---

## 6. 决策日志（每版本累加）

### v7 — 抽 MemoryProvider ABC
- **选**：抽抽象基类，所有 provider 实现 `is_available / initialize / system_prompt_block / get_tool_schemas / shutdown`。
- **没选**：用 protocol（duck typing）。原因：教学场景显式继承让"必须实现哪些方法"一目了然，IDE 也能直接报缺失。

### v9 — prefetch / sync_turn 钩子
- **选**：把"召回"和"持久化"分别放在 user message 注入前 / 一轮 tool loop 结束后。
- **没选**：让 provider 自己注册成"工具"让模型显式 call。原因：远端语义记忆的召回是隐式的（每轮都该有），让模型决定会丢召回 + 浪费 token。builtin 仍走 tool 路径作对照。

### v10 — HTTP 边界
- **选**：`RemoteSemanticProvider` + 单文件 mock FastAPI，存储用 `dict[session_id, list[(text, hash_vec)]]`。
- **没选**：直接接 Hindsight。原因：先在最小代价下验证"HTTP 边界形态对不对"，向量算法、存储引擎留到 v10.1。

### v10.1 — pgvector + OpenAI 范式 embedding
- **选 1**：`pgvector/pgvector:pg16` + docker-compose + `init.sql` 自动建表 + `ivfflat (vector_cosine_ops)`。
- **没选**：testcontainers / 仅给 schema.sql。原因：docker-compose 是教学受众最常见的"一键起依赖"形态。
- **选 2**：OpenAI SDK 范式 embedding，base_url 走配置切换。
- **没选**：直接调 DashScope/SiliconFlow 私有 SDK。原因：OpenAI 范式是事实标准，DeepSeek/SiliconFlow/DashScope/vLLM/llama.cpp 都兼容，换端点零成本。
- **选 3**：Provider 端只加配置项（`budget` / `min_score` / `auto_retain`），不改请求时序。
- **原因**：v10.1 的核心价值是验证 V7 抽 ABC 时承诺的"两端独立演化" — 服务端从 dict + hash → pgvector + OpenAI 全换，Provider 端业务逻辑 0 行变化。
- **真实踩坑（已修，commit `ca50210`）**：`init.sql` 写死 `VECTOR(1536)` 是 OpenAI text-embedding-3-small 的默认维度，但用户用 DashScope `text-embedding-v3`（1024 维），bug 直到第一次 `/sync` 才以 psycopg "expected 1536 dimensions, not 1024" 暴露。修复策略：在 `lifespan` 启动时读 `pg_attribute.atttypmod` 校验列实际维度 vs `EMBEDDING_DIM`，不一致就 `raise RuntimeError` + 打印可操作指引（改哪两个文件 + 跑哪两条 docker 命令）。**经验**：教学项目里"配置维度 vs schema 维度"这种隐式约束必须在启动期 fail-fast，否则下一个会话还会再踩。
- **选 4**：`/sync` 增加 LLM 事实抽取 + 向量去重（原 V12 候选，提前拉入 V10.1）。
- **没选**：继续存原始 "User: X\nAssistant: Y" 拼接文本。原因：原始对话 embedding 的召回精度差（一段长对话的平均向量模糊），且无法处理事实更新（"用户住北京"→"用户搬到上海"只会新增一行，不会覆盖旧行）。
- **实现**：`extract_facts()` 调对话模型抽取独立 fact → 每条 fact 独立 embed → 插入前查最相似已有 fact（cosine > 0.95 → UPDATE 而非 INSERT）。LLM 判断"无值得记住的内容"时返回空列表，跳过存储。
- **选 5**：builtin memory tool 增加 `read` action。
- **原因**：模型需要查看实时记忆状态（冻结快照只反映启动时的状态，mid-session 写入后快照不更新）。源项目注释提到 read 但实际未实现（因为 system prompt 已含快照），nano 补上让教学更完整。

### v11 — 知识图谱记忆（Hindsight 1:1 复现）
- **选 1**：服务端做实体/关系/事实三层抽取，替换 V10.1 的扁平 fact 抽取。
- **没选**：继续用扁平 fact 列表。原因：扁平 fact 与 builtin memory 功能重叠（都是存文本片段），无法体现 Hindsight 的核心价值 — 知识图谱的实体级去重和图遍历检索。
- **选 2**：多策略检索（语义搜索 + 实体匹配 + 1-hop 图遍历）。
- **没选**：纯余弦 top-k。原因：图遍历是知识图谱相对向量数据库的核心优势 — "问 A 的时候能召回 A 的关联实体 B 的信息"。
- **选 3**：新增 `/reflect` 端点（LLM 合成）。
- **原因**：Hindsight 的 reflect 是区别于普通 RAG 的关键特性 — 不是返回 hit 列表，而是跨记忆合成连贯回答。
- **选 4**：Provider 支持 memory_mode（context/tools/hybrid）。
- **没选**：只保留 context 模式（纯隐式 prefetch）。原因：源项目 Hindsight 的三种模式是其核心设计，hybrid 模式让模型既能被动接收召回，又能主动搜索/存储。
- **选 5**：暴露 `hindsight_retain` / `hindsight_recall` / `hindsight_reflect` 三个工具。
- **原因**：对齐源项目命名，让模型能主动存储重要信息、搜索记忆、请求合成回答。
- **DB schema**：banks + documents + entities + relations + facts 五表，对应 Hindsight 的数据模型。实体用 `UNIQUE(bank_id, name)` 做去重，关系用三元组唯一约束，facts 用同实体+高相似度做覆盖更新。

---

## 7. 待办 / 已知问题

- [ ] 仓库根有几个无关临时文件（`1.txt` / `2.txt` / `MCP_CLIENT_EXPLAINED.md`），不在 git 跟踪范围，需要时再清。
- [ ] `docs/` 下迭代规划是 `iteration-plan-v10.1.md`，但 skill 模板期望 `iteration-plan.md`（聚合所有版本）— 后续应建一份合并版规划。
- [x] `docs/memory-nano-vs-source.md` 已完成（记忆系统全景对比）。
- [ ] V11 知识图谱抽取 prompt 需要根据实际使用效果调优（当前是通用版）。
- [ ] V11 事实去重阈值 `FACT_DEDUP_THRESHOLD=0.92` 需要实测验证。
- [ ] V11 `/retain` 含 LLM 调用 + 多次 embedding，单次约 2-5s — 仍是 v12 异步化的入口痛点。
- [ ] V11 图遍历目前只做 1-hop，复杂场景可能需要 2-hop。
- [ ] V11 `docs/memory-nano-vs-source.md` 需要更新以反映 V11 的变化。

---

## 附：维护这个文件的硬规则

1. **每完成一版立即更新**：进度表打 ✅、决策日志加一段、cheatsheet 如有命令变化同步改。
2. **真实踩坑必记**：bug 修复 commit 后立即把"现象 → 根因 → 修复定位"写进对应版本的决策日志。
3. **路径全用绝对路径**：第二区块、cheatsheet 的所有路径都要绝对，新会话 cwd 不一定对。
4. **总长控制在 250 行内**：超过就该把"决策日志"按版本拆到 `docs/decisions/v<N>.md`，本文件只保留索引。
