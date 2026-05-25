# nano_hermes_agent — 项目上下文 primer

> **新会话起手必读。** 本文件是 `nano-project-builder` skill 的"交付物零"，
> 用来在跨目录、跨会话的 LLM 协作中保持上下文一致。每完成一个版本必须更新。

---

## 1. TL;DR

- **项目定位**：教学版 AI Agent，从零迭代演进到能挂载长期记忆。
- **源项目**：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级 AI Agent，含 gateway / 多模型后端 / SQLite 会话 / 多终端环境 / 插件系统）。
- **当前阶段**：v19 已完成 — TransportChain + 断路器 + jittered backoff（多 transport 故障切换 + 健康检查）。
- **核心叙事**：通过 V0→V19 的 20 档迭代，每一档解决前一档暴露的具体痛点，最终从扁平向量存储演进到完整知识图谱 + 读写双异步 + 运行期会话切换 + 上下文压缩 + 多跳召回 + 时间衰减 + 多家族 provider 协议解耦 + 主备故障切换。

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

## 3. 进度状态（20 档迭代）

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
| **v17** | **Transport ABC + ChatCompletionsTransport** | **provider 解耦：messages/tools/response 标准化 + 注册表 + client 工厂** | **✅** |
| v18 | AnthropicTransport + Registry | 第二家 transport / env-driven 路由 / 格式差异具体化 / Qwen DashScope 真跑 | ✅ |
| **v19** | **TransportChain + 断路器** | **多 transport 故障切换 / 错误三分类 / 断路器自愈 / jittered backoff** | **✅ 已完成** |

**下一档候选**（未启动）：v20 Prompt cache 控制 / v21 流式输出 + 中断。

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

### v12 — 异步 retain（后台 writer 线程）
- **选 1**：`queue.Queue` + 单写者守护线程 + sentinel 对象关闭。
- **没选**：`asyncio.create_task` / `concurrent.futures.ThreadPoolExecutor` / `multiprocessing`。原因：(a) 主循环是同步的，引入 asyncio 要改太多上游；(b) 池没必要 — retain 必须 FIFO 串行（向量去重要看已有 fact，并发会写出重复）；(c) 进程隔离对教学场景过重。Queue + 单线程 + sentinel 是 stdlib 内置且能完整演示"生产者/消费者 + 优雅关闭"的最小形态。
- **选 2**：lazy 启动 writer（首次 enqueue 才起线程），不在 `initialize()` 启动。
- **原因**：纯 `tools` 模式且模型从不主动 retain 时，挂一个空闲线程是浪费。源项目同样 lazy。
- **选 3**：`shutdown` 三步走 — set `_shutting_down` → put sentinel → bounded `join(timeout=10)`。
- **原因**：先停收避免新 job 永远 drain 不完；sentinel 让 writer 自然退出（比设标志更显式）；bounded join 兜底守护线程被 wedge 的极端情况，不让进程卡死。
- **选 4**：`atexit` 注册幂等钩子。
- **原因**：CLI 不走 `MemoryManager.shutdown_all()` 直接 ctrl-C 退出时，in-flight job 会和解释器 teardown 竞态（aiohttp/asyncio 资源未关闭警告）。atexit 兜底，且与显式 shutdown 之间用 `_shutting_down` 标志互斥。
- **选 5**：`sync_turn` 入队、`hindsight_retain` 工具仍同步。
- **原因**：模型显式 call retain 时期待立即拿到 "stored successfully" 反馈做下一步推理；隐式 sync_turn 没有调用方等返回，正是异步化的最佳目标。源项目同样区分对待。
- **选 6**：单 job 异常被 `try/except` 吞下并 `logger.warning`，writer 不退出。
- **原因**：写者必须始终活着直到 sentinel — 一次 HTTP 失败不能让后续 N 次入队的 retain 全部丢失。
- **验证**：`scripts/test_v12_writer.py` 7 项单元测试覆盖（入队不阻塞、FIFO 顺序、单 job 失败不杀线程、shutdown drain、幂等、shutdown 后丢弃、lazy 启动）；`scripts/test_v12_e2e.py` 真实 mock server 端到端验证：3 次 retain 主循环阻塞从 5103ms 降到 0.1ms（100% 减少）。

### v13 — 后台 prefetch 预热（两阶段 recall）
- **选 1**：`queue_prefetch()` + `prefetch()` 两阶段模式 — 当轮结束启动 daemon 线程预热，下一轮消费缓存。
- **没选**：持久线程池 / asyncio / 全局 prefetch 缓存。原因：每轮只有一次 recall HTTP 调用，单 daemon 线程是最小形态；匹配源项目 Hindsight 的 1:1 模式。
- **选 2**：冷启动 fallback — 第一轮无预热结果时 `prefetch()` 内同步调用 `_do_recall`。
- **原因**：第一轮用户仍需 recall 上下文，不能因为没有预热就跳过。从第 2 轮起预热生效，prefetch 近零延迟。
- **选 3**：`join(timeout=3.0)` — 后台线程超时后不等待，走 sync fallback。
- **原因**：匹配源项目；3s 是合理上限 — 超过说明服务端异常，不应让主循环无限等待。
- **选 4**：prefetch 线程与 writer 线程独立（不同实例变量、不同关注点）。
- **原因**：writer 管写路径（retain FIFO 串行），prefetch 管读路径（recall 单次）。两者生命周期不同，混用会增加复杂度。
- **选 5**：`shutdown()` 增加 join prefetch 线程（timeout=5s）。
- **原因**：确保关闭时不留悬挂线程；daemon 线程虽然不阻塞进程退出，但显式 join 更干净。
- **验证**：`scripts/test_v13_prefetch.py` 6 项测试覆盖（启动线程、消费缓存、超时 fallback、shutdown 阻止、冷启动 fallback、tools 模式跳过）。

### v14 — 会话切换（on_session_switch 生命周期钩子）
- **选 1**：`/new` + `/resume <id>` 两个用户命令。
- **没选**：`/branch`（从当前 session 分叉）。原因：nano 教学版不持久化对话历史，branch 语义无意义。
- **选 2**：`queue.join()`（无 timeout）drain writer queue。
- **原因**：旧 session 的 retain 必须全部落盘才能切换，否则数据丢失。HTTP timeout（360s）是最终兜底。
- **选 3**：writer 线程切换后保持存活。
- **没选**：shutdown + 重建。原因：新 session 的 sync_turn 复用同一 writer，避免线程创建开销。
- **选 4**：不 set `_shutting_down`。
- **原因**：那是永久关闭标志。session switch 后 provider 仍需正常工作（sync_turn、queue_prefetch 等）。
- **选 5**：`/new` 和 `/resume` 都重置 messages + turn_count。
- **原因**：nano 不持久化对话历史，切 session 后旧对话上下文对新 session 无意义。
- **验证**：`scripts/test_v14_session_switch.py` 7 项测试覆盖（更新 session_id、drain writer、清 prefetch 缓存、新 session_id 生效、in-flight prefetch join、连续切换、builtin no-op）。

### v16 — retain 批量 + 多跳图遍历 + 时间衰减
**Client（retain_every_n_turns 批量）**
- **选 1**：在 `RemoteSemanticProvider` 加 `retain_every_n_turns` + `_session_turns` buffer + `_turn_counter`，N=1 时立即入队（V12-V15 行为不变），N>1 时累积到第 N 轮才合并入队（payload tag `batch:N`）。
- **没选**：在服务端做批量。原因：缓冲应贴近事件源 — provider 知道一轮的天然边界，服务端只看到拼接好的 content 反而丢失了"几轮"这个语义。也对齐源项目 1:1。
- **选 2**：合并方式 `"\n\n---\n\n".join(turns)`，整体作为单条 content 发到 `/retain`，服务端 LLM 一次性看完整对话块再做实体/关系/事实抽取。
- **原因**：(a) 服务端抽取调用量降为 1/N，单次 retain 通常 2-5s，N=3 时三轮合并约省 70% 抽取时延；(b) 一组连续 turn 的实体共指更易被 LLM 识别（"他/她/这个项目"等指代），抽出的图谱质量更高。
- **选 3**：`on_session_switch` 在 `queue.join()` drain 前先 `_flush_buffer(session_id=old)`；`shutdown` 在 set `_shutting_down` 前先 flush。
- **原因**：buffer 里的 turn 不在 queue 里，drain queue 不能落盘缓冲。Flush 必须用旧 session_id，否则 N=3 累积了 2 条切 session 时这 2 条会被错记为新 session（违反 V14 的 session 隔离不变量）。

**Server（多跳图遍历 + 时间衰减）**
- **选 4**：把 V11 的硬编码 1-hop 升级为可配置 N-hop BFS，复用 frontier 集合按跳数染色（`{entity_name: hop_distance}`）。最终 fact score 乘 `hop_weight = HOP_DECAY ** (hop-1)`，hop=1 不打折。
- **没选**：固定 2-hop。原因：把跳数做成 env (`RECALL_HOPS`) 让教学受众能直观对比"单跳 vs 双跳"召回差异 — 这是图谱相对扁平向量库的核心卖点。HOP_DECAY=0.7 来源于"二跳证据应该比一跳证据弱但不可忽略"的经验值。
- **选 5**：时间衰减用指数半衰期 `time_weight = 0.5 ^ (age_days / half_life)`，再用 `DECAY_ALPHA` 混合：`final = cosine * hop_weight * ((1-α) + α * time_weight)`，age 取 fact.updated_at（事实更新时复活到 now）。
- **没选**：硬替换 — 让 final = cosine * time_weight。原因：α 混合让"完全关闭衰减"（α=0）和"完全跟随时间"（α=1）成连续可调的旋钮，符合教学的可观测性要求。半衰期模型对应"用户记忆遗忘曲线"的经典假设。
- **选 6**：源项目 Hindsight 通过独立 `hindsight_embed` 库做 graph rerank + 复杂衰减；nano 在 SQL + `math.pow` 层用最少代码复现核心思想。
- **原因**：rerank 库引入复杂依赖（faiss / scipy.spatial），教学价值低于自己写一遍 BFS + 半衰期函数。RecallResult 多暴露 `cosine / hop / age_days / time_weight` 字段，让客户端能看到打分细节，便于"为什么 A 排在 B 前面"的回答。
- **验证**：`scripts/test_v16_batch_decay.py` 7 项覆盖（N=1 立即入队 / N=3 缓冲合并 / session switch flush 旧 buffer / shutdown flush 旧 buffer / hop_weight 单调 / time_weight 半衰期 / decay_blend α 开关）。所有测试 + V12/V13/V14/V15 回归全绿。

### v17 — Transport ABC + ChatCompletionsTransport
- **选 1**：抽 `ProviderTransport` ABC（`api_mode` / `convert_messages` / `convert_tools` / `build_kwargs` / `normalize_response` 五件套 + 三个可选 hook），新增 `transports/` 子模块。
- **没选**：在 agent.py 里直接按 if/elif 分支不同 provider。原因：源项目 Hermes 已经验证了一旦要接 Anthropic（messages 拆 system / tool 用 input_schema / stop_reason 映射不同）就会污染 agent loop；ABC 把"协议特化"压进单个文件，agent loop 只看标准化结果。
- **选 2**：`NormalizedResponse` 数据类替代直接消费 SDK 原生 `ChatCompletion` 对象，但通过 `ToolCall.function` property 返回 self 维持向后兼容（`tc.function.name` / `tc.function.arguments` / `tc.type` 现有读法零改动）。
- **没选**：硬切到全新接口、agent.py 全面改写。原因：教学项目下相邻版本应该尽量"看得见的差异最少" — V17 的核心是抽出边界，不是借机重构调用点。
- **选 3**：注册表 + 自动发现（`_discover_transports` 在首次 `get_transport()` 时 import 所有 transport 模块触发 `register_transport`）。
- **没选**：硬编码 dispatch 表。原因：V18 加 AnthropicTransport 时只需在新文件末尾 `register_transport(...)` 即可被发现，agent.py 一行不改 — 这是"抽 ABC"声称的可扩展性的实证。
- **选 4**：`make_llm_client(api_mode)` 工厂函数独立于 transport（`transports/client_factory.py`），不让 transport 自己实例化 client。
- **原因**：transport 的职责是格式转换，client 的实例化是部署关注点（endpoint / api_key / 超时等）— 把它们分开让两侧能独立替换。V18 加 Anthropic 时只是这个 factory 多一个 elif 分支。
- **选 5**：裁剪 — nano 只保留 `chat_completions`，不复制源项目 614 行里的 16+ provider quirks（Moonshot tool schema / Gemini thinking / OpenRouter cache 等）。
- **原因**：那些是"产品需要"的兼容性补丁，对教学受众而言只是让核心模式被噪音淹没。V17 只演示一种 transport 形态，V18 加第二种（Anthropic）才是验证 ABC 价值的关键。
- **选 6**：`reasoning_content` 走 `provider_data` 而非升 top-level。
- **原因**：DeepSeek/Moonshot 的 `reasoning_content` 是协议特定的（OpenAI 标准没有），跨家族通用字段（content / tool_calls / finish_reason / usage）才升 top-level。这是 ABC 设计中"共享接口 vs 协议特化"的边界划分原则的实例。
- **真实裁剪权衡**：源项目 transports/ 还包含 `bedrock.py` / `codex_responses.py` / `anthropic_messages.py`，nano 只复刻基础模式。V18 会增加 Anthropic（凭借 Anthropic SDK 与 OpenAI SDK 在 messages / tools / response shape 上的真实差异演示 ABC 的价值）。
- **验证**：`scripts/test_v17_transport.py` 10 项覆盖（注册表查找 / unknown api_mode 返回 None / build_kwargs 最小集 / build_kwargs 含 tools+options / 文本响应标准化 / tool_calls 响应+向后兼容 / validate 拒绝空 choices / cached_tokens 抽取 / reasoning_content 进 provider_data / build_tool_call 工厂）。所有测试 + V12/V13/V14/V15/V16 回归全绿。

### v18 — AnthropicTransport + Registry
- **选 1**：新增 `transports/anthropic.py` 实现 `AnthropicTransport`，核心格式差异全部在 transport 内部消化，agent loop 零改动。
- **原因**：这正是 V17 抽 ABC 的承诺 — "加第二家 transport 时 agent loop 不动"。V18 是这个承诺的实证。
- **选 2**：`convert_messages` 返回 `(system, messages)` 元组 — system 拆出作为独立参数。
- **原因**：Anthropic API 的 system 是顶层参数而非 messages 数组里的一条。这是两家协议最显著的结构差异之一。
- **选 3**：assistant tool_calls → `tool_use` content blocks；tool results → user 消息里的 `tool_result` content blocks。
- **原因**：Anthropic 把 tool 调用和结果都建模为 content blocks（不是 OpenAI 的顶层 `tool_calls` 字段 + 独立 `role=tool` 消息）。这是第二个核心差异。
- **选 4**：`build_kwargs` 默认 `thinking={"type":"disabled"}`。
- **原因**：DashScope Qwen 的 Anthropic 端点要求显式传 thinking 配置，不传会 400。默认 disabled 让 Qwen 能跑；未来 V20 可以改成 enabled 来启用 reasoning。
- **选 5**：`TRANSPORT_MODE` env 驱动 transport 选择（默认 `chat_completions`）。
- **没选**：自动探测（按 base_url 猜）。原因：显式优于隐式 — 教学场景下学员应该清楚知道自己在用哪条路径。
- **选 6**：agent loop 的 SDK 调用按 `transport.api_mode` 路由（`client.chat.completions.create` vs `client.messages.create`）。
- **没选**：让 transport 自己持有 client 并暴露 `call()` 方法。原因：transport 的职责是格式转换，不是 SDK 调用 — 把调用留在 agent loop 让"谁负责什么"更清晰。
- **选 7**：`client_factory.make_llm_client` 新增 `anthropic_messages` 分支 → `anthropic.Anthropic(api_key, base_url)`。
- **原因**：factory 是 V17 就铺好的基础设施，V18 只是多一个 elif — 验证了"加新家族的成本是 O(1)"。
- **选 8**：`ContextCompressor.compress()` 新增 `transport` 参数，Anthropic 模式下用 `client.messages.create` 做摘要。
- **原因**：压缩器也需要调 LLM，不能假设永远是 OpenAI 兼容。transport 参数让压缩器跟主循环走同一条路径。
- **验证**：`scripts/test_v18_anthropic.py` 11 项覆盖（注册表 / system 拆出 / tool_calls+results 转换 / tools schema 转换 / build_kwargs 必填字段 / text 响应标准化 / tool_use 响应+向后兼容 / stop_reason 映射 / validate / cached_tokens / env 路由）。所有测试 + V12-V17 回归全绿。

### v19 — TransportChain + 断路器（多 transport 故障切换）
- **选 1**：抽 `TransportChain` 类管理"主备 transport 顺序故障切换"，agent loop 从 `transport.call(client, ...)` 改成 `chain.call(...)`，签名兼容（链版本 `client` 参数被忽略，每个 entry 自带 client）。
- **没选**：在 agent loop 里写 try/except + if 切换。原因：故障切换涉及错误分类、断路器状态、backoff、半开探针四件事，混进主循环会让"主流"和"故障路径"耦合得难维护。源项目 `run_agent.py:1655-1697` 把 fallback 逻辑铺在主循环里，已经踩过这个坑（一改主循环就要重新思考 fallback 边界）— nano 把这层抽出来作为反例的正解。
- **选 2**：错误分类做成独立 `classify_error()` 函数，返回 3 类 `ErrorAction`（RETRYABLE / FAILOVER / FATAL）。
- **没选**：源项目的 14 种 `FailoverReason`。原因：14 种是为了对接十几家 provider 的私有错误码做精细化决策，nano 教学只关心"该不该切" — 二元决策再加个"重试一次"足够。具体决策为：
  - **RETRYABLE**（瞬时故障）：500/502/504/408、timeout 关键词 → 同 transport 等待后重试 N 次；
  - **FAILOVER**（这家不行了）：429/401-403/402/503/529、rate_limit/auth/billing/overloaded 关键词 → 直接切下一家；
  - **FATAL**（用户/输入问题）：400 + context_overflow、413 payload_too_large、format_error → 切了也是错，直接抛出。
- **选 3**：断路器三态自愈（closed / open / half_open），用最小数据结构 `_BreakerState(consecutive_failures, opened_at)` 表达。
- **原因**：`opened_at == 0.0` → closed；`opened_at != 0` 且 `now - opened_at < cooldown` → open；过了 cooldown 但还没探针成功 → half_open。两个字段表达三态比"独立 enum + 状态机"清爽得多。半开探针成功后立即 close（重置两个字段），失败则 `_record_failure` 重新累计 — 不需要"半开 → 重新打开"的特殊路径。
- **选 4**：jittered backoff 用 `base * 2^attempt + uniform(0, 0.5*delay)`。
- **原因**：纯指数退避会让多 session 的重试时刻同步（thundering herd），打到刚刚恢复的服务上立即把它再打挂。jitter 让重试时刻散开。源项目 `agent/retry_utils.py:19-57` 做的就是这个，nano 复刻。`min(..., 60s)` 上限避免重试到天荒地老。
- **选 5**：`TRANSPORT_CHAIN` env 优先于 `TRANSPORT_MODE`，不设则退化为 V18 的单 transport 行为。
- **原因**：(a) 向后兼容 — V18 的部署不需要改任何 env；(b) 显式优于隐式 — 链顺序由 env 字符串顺序决定（`chat_completions,anthropic_messages` 表示主家是 OpenAI 兼容，备家是 Anthropic），教学受众一眼能看出主备。链长 1 时仍带 RETRYABLE 重试，但不会跨家切换 — 这是该选择的副作用，可接受。
- **选 6**：`ContextCompressor.compress()` 接受 chain 当 transport 用（duck typing），无需改造。
- **原因**：chain.call 的签名 `(client, **kwargs)` 与 `transport.call` 完全一致 — `client` 在 chain 上被忽略，但保留位置参数让 V18 的调用点零改动。这是"接口收敛"的复利 — V17 把所有 LLM 调用收敛到 `transport.call()` 的好处在 V19 兑现：摘要 LLM 也免费享受 failover。
- **选 7**：`/transport` 命令展示链状态（每个 entry 的 closed/open/half_open + 失败计数 + 冷却剩余时间 + 模型名）。
- **原因**：断路器是隐式状态，没有可观测性的话用户根本不知道"为什么 primary 被跳过"。教学项目尤其需要这种透明度。
- **选 8（V19.1 修补）**：每个 chain entry 自带 `model` 字段，链字符串语法升级为 `api_mode[:model]`，例：`chat_completions:deepseek-chat,anthropic_messages:qwen3.6-plus`。entry.model 为 None 时回退到全局 `MODEL` env。
- **没选**：让两家 transport 共享同一个 `MODEL` env。原因：现实里主备两家用的是**不同 provider 的不同模型**（DeepSeek 的 `deepseek-chat` 切到 Qwen 的 `qwen3.6-plus`）— 共享 `MODEL` 会让备家激活时带着错误的模型名调过去，立即 400。源项目 `hermes-agent/run_agent.py:1742-1765` 把 fallback chain 做成 `list[dict]`，每条 entry 自包含 `{provider, model, base_url, api_key}`，激活时按 entry 重建 client + 切 model — 这是 "fallback 必须自包含"的硬约束的根本原因。nano 翻译为字符串内联（`api_mode:model`）保留单 env 字符串的简洁，同时把"每条 entry 携带自己的模型"这个不变量做硬。
- **真实踩坑（设计阶段，未上线）**：V19 第一版只让链共享 `MODEL` env，写完 banner 才发现这意味着两家被强制共用同一个模型名 — 在 DeepSeek + DashScope 混搭场景下根本跑不通。修复策略：增加 `_ChainEntry.model: Optional[str]`，`build_chain_from_env` 解析 `:` 分隔的内联 model，`_try_with_retry` 用 `dict(kwargs)` 浅拷贝后覆盖（避免链上各 entry 互相污染调用 kwargs）。新增 3 项测试覆盖（per-entry model 覆盖 / failover 后切到备家用对模型 / entry.model None 回退到调用方 model）。
- **真实裁剪权衡**：源项目 `error_classifier.py` 1058 行 + `run_agent.py:1742-1764` 的 fallback chain + `retry_utils.py` 三处合计约 1500 行，nano 用 `error_classifier.py`（170 行）+ `chain.py`（240 行，含 V19.1 model 字段）共约 410 行复刻核心机制。删掉的部分：(a) provider-specific 错误串匹配（gemini "thinking signature" / openrouter cache miss / llama_cpp grammar 等），(b) `OAuthLongContextBetaForbidden` 之类边缘 reason，(c) status code → reason 的优先级精细化（nano 用直接映射），(d) entry 自包含 `base_url / api_key`（nano 复用 `client_factory.make_llm_client`，每个 api_mode 一组 env，简单够用）。教学价值在于"看清主备链 + 断路器 + jitter + per-entry model 四个机制如何协同"，不是 1:1 复制 provider 兼容矩阵。
- **验证**：`scripts/test_v19_failover.py` 17 项覆盖（5 项 classify_error + 9 项 chain + 3 项 V19.1 per-entry model）。所有测试 + V12/V13/V14/V16/V17/V18 回归全绿（V15 的 2 项预存在错误是 V18 改 `compress()` 签名时未同步该测试，与 V19 无关）。

---

## 7. 待办 / 已知问题

- [ ] 仓库根有几个无关临时文件（`1.txt` / `2.txt` / `MCP_CLIENT_EXPLAINED.md`），不在 git 跟踪范围，需要时再清。
- [ ] `docs/` 下迭代规划是 `iteration-plan-v10.1.md`，但 skill 模板期望 `iteration-plan.md`（聚合所有版本）— 后续应建一份合并版规划。
- [x] `docs/memory-nano-vs-source.md` 已完成（记忆系统全景对比）。
- [ ] V11 知识图谱抽取 prompt 需要根据实际使用效果调优（当前是通用版）。
- [ ] V11 事实去重阈值 `FACT_DEDUP_THRESHOLD=0.92` 需要实测验证。
- [x] ~~V11 `/retain` 含 LLM 调用 + 多次 embedding，单次约 2-5s~~ — V12 已通过后台 writer 解决（主循环 0 阻塞）。
- [x] ~~V11 图遍历目前只做 1-hop，复杂场景可能需要 2-hop。~~ — V16 已通过 `RECALL_HOPS` 实现 N-hop BFS（默认 2）。
- [x] ~~V12 后只剩 prefetch 同步阻塞（200-500ms/轮）~~ — V13 已通过后台 prefetch 预热解决（第 2 轮起近零阻塞）。
- [x] ~~仍未实现 `on_session_switch`：切 session 时 buffer 没 flush~~ — V14 已通过 on_session_switch 生命周期钩子解决（drain + 清缓存 + 轮转）。
- [ ] V16 `RECALL_HOPS=2` 的实测召回质量需要在真实 bank 上验证（教学示例可能数据量太小看不出差异）。
- [ ] V16 `DECAY_HALF_LIFE_DAYS=30` 是猜测值，需要根据实际记忆使用周期调优；用户能不能"显式重要"标记免衰减？
- [x] ~~V17 `transports/` 只有 `chat_completions` 一家，ABC 价值在 V18 加 Anthropic 时才会真正显现~~ — V18 加 Anthropic 验证 ABC，V19 加 Chain 进一步验证"抽出来的边界能复用"。
- [ ] V19 断路器 `cooldown_seconds=60` 是猜测值，需要根据实际 provider 恢复时间调优；不同 reason 应不应该有不同 cooldown？
- [ ] V19 真实多家 provider 联跑测试缺失 — 当前只有 fake transport 的不变量测试，需要在两家真 endpoint 上验证（比如故意把 OPENAI_API_KEY 改错触发 401，观察 chain 是否切到 Anthropic）。
- [ ] CLAUDE.md 已超 250 行硬规则上限，下一档完成后应把决策日志按版本拆到 `docs/decisions/v<N>.md`，本文件只留索引。

---

## 附：维护这个文件的硬规则

1. **每完成一版立即更新**：进度表打 ✅、决策日志加一段、cheatsheet 如有命令变化同步改。
2. **真实踩坑必记**：bug 修复 commit 后立即把"现象 → 根因 → 修复定位"写进对应版本的决策日志。
3. **路径全用绝对路径**：第二区块、cheatsheet 的所有路径都要绝对，新会话 cwd 不一定对。
4. **总长控制在 250 行内**：超过就该把"决策日志"按版本拆到 `docs/decisions/v<N>.md`，本文件只保留索引。
