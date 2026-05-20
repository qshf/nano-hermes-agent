# nano_hermes_agent — 项目上下文 primer

> **新会话起手必读。** 本文件是 `nano-project-builder` skill 的"交付物零"，
> 用来在跨目录、跨会话的 LLM 协作中保持上下文一致。每完成一个版本必须更新。

---

## 1. TL;DR

- **项目定位**：教学版 AI Agent，从零迭代演进到能挂载长期记忆。
- **源项目**：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级 AI Agent，含 gateway / 多模型后端 / SQLite 会话 / 多终端环境 / 插件系统）。
- **当前阶段**：v10.1 已完成 — `remote_semantic` 内存版 → PostgreSQL + pgvector + OpenAI 范式 embedding。本版本踩过维度硬编码 bug，已加启动期自检。
- **核心叙事**：通过 V0→V10.1 的 11 档迭代，每一档解决前一档暴露的具体痛点，最后兑现 V7 抽 ABC 时承诺的"两端独立演化"。

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

## 3. 进度状态（11 档迭代）

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
| **v10.1** | **pgvector + OpenAI embedding** | **真实向量存储 + 范式 embedding** | **✅ 已合入** |

**下一档候选**（未启动）：v11 异步 retain（后台线程 + 队列）；v12 LLM 中间层做事实抽取。

---

## 4. 环境前置

### 4.1 必填 env（agent 主进程）
```bash
OPENAI_API_KEY=...           # 对话模型 key（DeepSeek/OpenAI/...）
OPENAI_BASE_URL=...          # 对话端点
MODEL=deepseek-chat          # 模型名
```

### 4.2 v10.1 新增（mock server 端，agent 主进程不需要）
```bash
DATABASE_URL=postgresql://nano:nano@127.0.0.1:5432/nano_memory
EMBEDDING_API_KEY=...        # 默认 fallback 到 OPENAI_API_KEY
EMBEDDING_BASE_URL=...       # 默认 fallback 到 OPENAI_BASE_URL
EMBEDDING_MODEL=text-embedding-v3   # 当前实测：DashScope
EMBEDDING_DIM=1024            # ⚠️ 必须与 init.sql 的 VECTOR(N) 一致
```

### 4.3 外部依赖
- **Postgres + pgvector**：通过 `docker-compose.yml` 起 `pgvector/pgvector:pg16`（已在跑：容器名 `nano-memory-pg`，端口 `127.0.0.1:5432`）。
- **mock memory server**：`scripts/mock_memory_server.py`（FastAPI + psycopg），监听 `127.0.0.1:8765`。

### 4.4 启用远端记忆（agent 端）
```bash
MEMORY_SERVICE_URL=http://127.0.0.1:8765   # 不设则只挂 builtin provider
MEMORY_SESSION_ID=default
```

---

## 5. 验活 cheatsheet

```bash
# 当前在哪个分支
git -C /Users/qshf/my-project/nano_hermes_agent branch --show-current

# DB 容器健康吗
docker ps --filter name=nano-memory-pg --format "table {{.Names}}\t{{.Status}}"

# DB 当前向量维度（必须与 EMBEDDING_DIM 一致）
docker exec nano-memory-pg psql -U nano -d nano_memory -c "\d memories" | grep embedding

# DB 累计了多少条记忆
docker exec nano-memory-pg psql -U nano -d nano_memory -c "SELECT count(*) FROM memories;"

# mock server 起没起、健不健康
curl -s --noproxy '*' http://127.0.0.1:8765/healthz | python -m json.tool

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

---

## 7. 待办 / 已知问题

- [ ] 仓库根有几个无关临时文件（`1.txt` / `2.txt` / `MCP_CLIENT_EXPLAINED.md`），不在 git 跟踪范围，需要时再清。
- [ ] `docs/` 下迭代规划是 `iteration-plan-v10.1.md`，但 skill 模板期望 `iteration-plan.md`（聚合所有版本）— 后续若有 v11 应该建一份合并版规划。
- [ ] `docs/v10.1-vs-v10.md` 是版本间差异，未来还需 `docs/nano-vs-source.md`（nano 最终版 vs 源项目 hermes-agent 全景对比）。
- [ ] v10.1 的 `auto_retain=False` 路径未做端到端验证（只在代码里留了开关）。
- [ ] mock server `/sync` 同步阻塞，单次约 200-500ms — 这是 v11 的入口痛点，提前埋点观察。

---

## 附：维护这个文件的硬规则

1. **每完成一版立即更新**：进度表打 ✅、决策日志加一段、cheatsheet 如有命令变化同步改。
2. **真实踩坑必记**：bug 修复 commit 后立即把"现象 → 根因 → 修复定位"写进对应版本的决策日志。
3. **路径全用绝对路径**：第二区块、cheatsheet 的所有路径都要绝对，新会话 cwd 不一定对。
4. **总长控制在 250 行内**：超过就该把"决策日志"按版本拆到 `docs/decisions/v<N>.md`，本文件只保留索引。
