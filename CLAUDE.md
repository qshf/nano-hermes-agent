# nano_hermes_agent — 项目上下文 primer

> **新会话起手必读。** 本文件是 `nano-project-builder` skill 的"交付物零"，
> 用来在跨目录、跨会话的 LLM 协作中保持上下文一致。每完成一个版本必须更新。

---

## 1. TL;DR

- **项目定位**：教学版 AI Agent，从零迭代演进到能挂载长期记忆。
- **源项目**：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级 AI Agent，含 gateway / 多模型后端 / SQLite 会话 / 多终端环境 / 插件系统）。
- **当前阶段**：v21.3 已完成 — Skill 系统（progressive disclosure tier 1 索引 + tier 2 `skill_view` 工具 + `/skill` 命令 + plan/TDD/systematic-debugging 三个示例）。
- **核心叙事**：通过 V0→V21.3 的 24 档迭代，每一档解决前一档暴露的具体痛点，最终从扁平向量存储演进到完整知识图谱 + 读写双异步 + 运行期会话切换 + 上下文压缩 + 多跳召回 + 时间衰减 + 多家族 provider 协议解耦 + 主备故障切换 + 显式 prompt cache + 交互层装饰器注册 + 三段式 prompt + skill 渐进式披露。

---

## 2. 路径与仓库

| 角色 | 绝对路径 | git remote | 主分支 |
|------|---------|-----------|-------|
| **源项目** | `/Users/qshf/my-project/hermes-agent` | `https://github.com/qshf/hermes-agent` | `main` |
| **nano 项目** | `/Users/qshf/my-project/nano_hermes_agent` | `git@github.com:qshf/nano-hermes-agent.git` | 多分支 `v0`..`v10.1`，无 main |
| **当前活跃分支** | `skill/v0.21.3`（V21 系列：v21.1/v21.2 已合并到 transport/v0.20，v21.3 在自身分支待 review） | — | — |

**跨目录的硬约束**：源项目和 nano 不在同一目录。任何"对照源项目读 X 文件"的操作都必须用源项目的绝对路径，例：
- 源项目 Hindsight 插件：`/Users/qshf/my-project/hermes-agent/plugins/memory/hindsight/__init__.py`
- 源项目记忆迭代规划：`/Users/qshf/my-project/hermes-agent/docs/memory-system/iteration-plan.md`

---

## 3. 进度状态（24 档迭代）

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
| v11 | 知识图谱记忆（Hindsight 1:1） | 实体/关系/事实抽取 + 多策略检索 + reflect 合成 + memory mode | ✅ |
| v12 | 异步 retain（后台 writer 线程） | queue + sentinel 优雅关闭 + lazy 启动 + atexit 兜底 | ✅ |
| v13 | 后台 prefetch 预热 | queue_prefetch + 两阶段消费 + 冷启动 fallback + join timeout | ✅ |
| v14 | 会话切换（on_session_switch） | /new + /resume 命令 + drain writer + 清 prefetch 缓存 + 轮转 session_id | ✅ |
| v15 | 上下文压缩（on_pre_compress） | 五阶段压缩流水线 + 抢救对话进 retain 队列 | ✅ |
| v16 | retain 批量 + 多跳 + 时间衰减 | retain_every_n_turns 缓冲 / N-hop BFS 图遍历 / 半衰期指数衰减 | ✅ |
| v17 | Transport ABC + ChatCompletionsTransport | provider 解耦：messages/tools/response 标准化 + 注册表 + client 工厂 | ✅ |
| v18 | AnthropicTransport + Registry | 第二家 transport / env-driven 路由 / 格式差异具体化 / Qwen DashScope 真跑 | ✅ |
| v19 | TransportChain + 断路器 | 多 transport 故障切换 / 错误三分类 / 断路器自愈 / jittered backoff | ✅ |
| v20 | Prompt Cache 控制（Anthropic ephemeral） | system_and_3 cache_control 注入 / Usage 拆 read+write / chain 累计命中率 / /transport 展示 | ✅ |
| v21.1 | slash 命令注册表 | 装饰器 + AgentCtx + dispatch / main.py 主循环瘦身 ~200 行 / agent.py → main.py | ✅ |
| v21.2 | 三段式 PromptBuilder | 骨架 / skill 索引段（占位）/ memory / 工具列表 — 段顺序固定保 V20 cache prefix | ✅ |
| **v21.3** | **Skill 系统（progressive disclosure）** | **tier 1 索引（name+desc 注入 prompt）+ tier 2 `skill_view` 工具 + `/skill` 命令 + 3 示例** | **✅ 已完成** |

**下一档候选**（未启动）：v22 流式输出 + 中断 / v23 多 agent 协作。

---

## 4. 环境前置

### 4.1 必填 env（agent 主进程）
```bash
OPENAI_API_KEY=...           # 对话模型 key（DeepSeek/OpenAI/...）
OPENAI_BASE_URL=...          # 对话端点
MODEL=deepseek-chat          # 模型名
TRANSPORT_MODE=chat_completions  # V18: chat_completions（默认）/ anthropic_messages
```

### 4.1.0 V19 故障切换（可选，多家 transport 时启用）
```bash
# 链段语法：api_mode[:model] — 每个 entry 自包含模型名（仿源项目 fallback chain）
TRANSPORT_CHAIN=chat_completions:deepseek-chat,anthropic_messages:qwen3.6-plus
# 不内联 model 时回退到全局 MODEL env：
# TRANSPORT_CHAIN=chat_completions,anthropic_messages   # 两家共享 MODEL（一般不实用）
FAILOVER_FAILURE_THRESHOLD=3        # 断路器打开阈值（连续失败次数）
FAILOVER_COOLDOWN_SECONDS=60        # 断路器冷却时间
FAILOVER_MAX_RETRIES=2              # 单 transport RETRYABLE 错误最大重试次数
FAILOVER_BASE_DELAY=1.0             # backoff 基数（实际延迟 = base * 2^attempt + jitter）
```

### 4.1.1 V18 Anthropic 模式（TRANSPORT_MODE=anthropic_messages 时必填）
```bash
ANTHROPIC_API_KEY=...        # DashScope API key 等
ANTHROPIC_BASE_URL=https://dashscope.aliyuncs.com/apps/anthropic
MODEL=qwen3.6-plus           # DashScope Anthropic 端点支持的模型
```

### 4.1.2 V20 Prompt Cache（可选，启用后 Anthropic transport 自动打 cache_control）
```bash
PROMPT_CACHE_ENABLED=1       # 1/0；启用后 chain 在每次调用前调 transport.apply_prompt_cache
PROMPT_CACHE_TTL=5m          # 5m（默认）/ 1h；1h 单价更高但 TTL 长
# 注意：仅 Anthropic transport 实际打标记；ChatCompletions 是 identity 直通
# （DeepSeek/OpenAI 用 prefix 匹配自动缓存，不需调用方主动标记）
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
MEMORY_RETAIN_EVERY_N_TURNS=1               # V16: N>1 时缓冲 N 轮再合并 retain
```

### 4.5 v16 新增（mock server 端）
```bash
RECALL_HOPS=2                # 多跳图遍历跳数；1=单跳（V11 行为）
HOP_DECAY=0.7                # 跨跳权重衰减系数；hop=N 的 fact 权重 = 0.7^(N-1)
DECAY_HALF_LIFE_DAYS=30      # 时间衰减半衰期；<=0 关闭
DECAY_ALPHA=0.3              # decay 在最终 score 中的混合权重；0 关闭
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

# V21.3 — 看当前 nano 装了几个 skill
ls /Users/qshf/my-project/nano_hermes_agent/skills/

# V21.3 — 跑 skill 系统不变量
.venv/bin/python /Users/qshf/my-project/nano_hermes_agent/scripts/test_v21_3_skill.py
```

---

## 6. 决策日志（按版本拆分）

每档版本的"为什么这样选 / 没那样选 / 真实踩坑 / 验证方式"独立成文，索引见
[docs/decisions/README.md](docs/decisions/README.md)。新版本完成后在那里追加 `v<N>.md`。

> **维护规则**：决策日志只增不删；如发现旧选择被新版本推翻，**保留旧记录 + 在新版本日志里写"为什么改"**，让"曾经踩过这个坑"的历史可追溯。

## 7. 待办 / 已知问题

迁移到 [docs/todo.md](docs/todo.md)。新增/勾选请直接编辑该文件。

---

## 附：维护这个文件的硬规则

1. **每完成一版立即更新**：进度表打 ✅、cheatsheet 如有命令变化同步改、`docs/decisions/v<N>.md` 创建并加入索引、`docs/todo.md` 勾选已解决项 + 追加新发现的待办。
2. **真实踩坑必记**：bug 修复 commit 后立即把"现象 → 根因 → 修复定位"写进对应版本的 `docs/decisions/v<N>.md`。
3. **路径全用绝对路径**：第二区块、cheatsheet 的所有路径都要绝对，新会话 cwd 不一定对。
4. **总长控制在 250 行内**：本文件只放索引 + 不变区（TL;DR / 路径 / 进度 / env / cheatsheet）。决策日志、待办均已外部化。
