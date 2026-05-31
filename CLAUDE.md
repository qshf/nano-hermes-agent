# nano_hermes_agent — 项目上下文 primer

> **新会话起手必读。** 跨目录、跨会话保持上下文一致。每完成一档必须更新。

---

## 1. TL;DR

- **项目定位**：教学版 AI Agent，从零迭代演进到挂载长期记忆 + 多智能体 + 跨项目可用。
- **源项目**：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级，含 gateway / 多模型后端 / SQLite 会话 / 多终端环境 / 插件系统）。
- **当前阶段**：v24.0 — 会话状态持久化（SQLite 会话子系统 + 真 resume）。详见 [docs/decisions/v24.0.md](docs/decisions/v24.0.md)。
- **演进主轴**：内存（v6→v16）→ transport（v17→v20）→ 交互层（v21.x）→ 流式（v22）→ 多智能体（v23.x）→ 会话持久化（v24.x）。

---

## 2. 路径与仓库

| 角色 | 绝对路径 | git remote |
|------|---------|-----------|
| 源项目 | `/Users/qshf/my-project/hermes-agent` | `https://github.com/qshf/hermes-agent` |
| nano | `/Users/qshf/my-project/nano_hermes_agent` | `git@github.com:qshf/nano-hermes-agent.git` |
| 当前分支 | `delegate/v0.23.4`（基于 v0.23.1 + v15.1 修复档；v23.3/v23.4 已各建同名分支指针，均推远端） | — |

**跨目录硬约束**：源项目和 nano 不在同一目录。"对照源项目读 X 文件"的操作必须用源项目绝对路径，例如 `/Users/qshf/my-project/hermes-agent/plugins/memory/hindsight/__init__.py`。

---

## 3. 进度状态（29 档）

每档"引入概念"只列关键词；细节看 [docs/decisions/v<N>.md](docs/decisions/README.md)。

| 版本 | 标题 | 关键词 |
|------|------|--------|
| v0–v5 | 工具调度演进 | if/elif → 注册表 → tool calling → toolsets → prompt builder → MCP |
| v6–v8 | 记忆基础设施 | builtin store → MemoryProvider ABC → MemoryManager 编排 |
| v9 | Agent Loop 生命周期 | prefetch / sync_turn 钩子 |
| v10 / v10.1 | 远端语义记忆 | HTTP 边界 → pgvector + OpenAI embedding |
| v11 | 知识图谱（Hindsight 1:1） | 实体/关系/事实 + 多策略检索 + reflect + memory mode |
| v12 / v13 | 读写双异步 | 后台 writer 线程 + 后台 prefetch 预热 |
| v14 / v15 / v15.1 | 会话与压缩 | /new + /resume → 5 阶段压缩 → 边界修复 + assistant 合法性 |
| v16 | 召回质量 | retain 批量 / N-hop BFS / 时间衰减 |
| v17 / v18 / v19 | Transport 解耦 | ABC → AnthropicTransport → Chain + 断路器 |
| v20 | Prompt Cache | Anthropic ephemeral system_and_3 / Usage 拆 read+write |
| v21.1 / v21.2 / v21.3 / v21.4 | 交互层 | slash 注册表 → 三段式 PromptBuilder → Skill progressive disclosure → tool result 协议 |
| v22 | 流式 + 中断 | stream_call / CancelToken / failover-before-first-event |
| v23.0 / v23.1 | 多智能体 | delegate_task + 隔离 child loop → tasks[] 批量并行 + 工具白名单 |
| v23.2 | 跨项目可用 | `--cwd PATH` + nano-hermes-agent.md / AGENTS.md 注入 / NANO_IGNORE_RULES |
| v23.3 | 多智能体流式中继 + 父子 cancel | stream_enabled 透传到 child_loop / progress 走 stderr / 共享 CancelToken / interrupted 状态 |
| v23.4 | 多智能体结构化结果 + 成本聚合 | 统一 `{"results":[...]}` JSON / runtime.session_tokens 4 维 / tool_trace + duration / `/transport` 末尾 session 行 |
| **v24.0** | **会话状态持久化（SQLite + 真 resume）** | **`agent/session_store.py`：sessions + messages 两表 / WAL / 全量删重插 / 剔除 system / 轮末+退出 save / `/resume` 真 load / `/sessions` 列表** |

**下一档候选**：v24.1 append-only 游标 + 压缩链（会话分裂 + parent 串链 + resume 重定向到 tip）/ v15.2 prefill retry / v23.5 嵌套 delegate（role: orchestrator + max_spawn_depth）。

---

## 4. 环境前置

### 4.1 必填 env（agent 主进程）
```bash
OPENAI_API_KEY=...                  # 对话模型 key
OPENAI_BASE_URL=...                 # 对话端点
MODEL=deepseek-chat                 # 模型名
TRANSPORT_MODE=chat_completions     # chat_completions / anthropic_messages
```

### 4.2 可选 env（按版本聚合）
```bash
# V18 Anthropic（TRANSPORT_MODE=anthropic_messages 时必填）
ANTHROPIC_API_KEY=...; ANTHROPIC_BASE_URL=https://dashscope.aliyuncs.com/apps/anthropic
MODEL=qwen3.6-plus

# V19 故障切换
TRANSPORT_CHAIN=chat_completions:deepseek-chat,anthropic_messages:qwen3.6-plus
FAILOVER_FAILURE_THRESHOLD=3 ; FAILOVER_COOLDOWN_SECONDS=60
FAILOVER_MAX_RETRIES=2 ; FAILOVER_BASE_DELAY=1.0

# V20 Prompt Cache（仅 Anthropic 实际打标记）
PROMPT_CACHE_ENABLED=1 ; PROMPT_CACHE_TTL=5m   # 5m 默认 / 1h 单价高 TTL 长

# V22 流式 + 中断
STREAM_ENABLED=1   # 0 退化到 V21 同步路径；运行期 /stream on|off 切换

# V23.1 批量 delegate
DELEGATE_MAX_CONCURRENT=3   # ThreadPoolExecutor max_workers；非数字/<=0 兜底 3

# V23.2 项目上下文（详见 v23.2 决策日志）
NANO_IGNORE_RULES=0   # 1 时跳过 nano-hermes-agent.md / AGENTS.md 注入

# V15 上下文压缩
CONTEXT_WINDOW=32000 ; CONTEXT_THRESHOLD_PERCENT=0.75
CONTEXT_PROTECT_HEAD=3 ; CONTEXT_TAIL_BUDGET=4000

# 远端记忆（设了 MEMORY_SERVICE_URL 才挂 remote_semantic provider）
MEMORY_SERVICE_URL=http://127.0.0.1:8765 ; MEMORY_SESSION_ID=default
MEMORY_BANK_ID=hermes ; MEMORY_MODE=hybrid          # context / tools / hybrid
MEMORY_PREFETCH_METHOD=recall ; MEMORY_RECALL_BUDGET=mid    # low / mid / high
MEMORY_AUTO_RETAIN=1 ; MEMORY_AUTO_RECALL=1
MEMORY_RETAIN_TAGS= ; MEMORY_RETAIN_EVERY_N_TURNS=1   # V16 缓冲 N 轮再合并
```

### 4.3 mock server 端 env（``scripts/mock_memory_server.py``）
```bash
DATABASE_URL=postgresql://nano:nano@127.0.0.1:5432/nano_memory
EMBEDDING_API_KEY=... ; EMBEDDING_BASE_URL=...     # fallback 到 OPENAI_*
EMBEDDING_MODEL=text-embedding-v3 ; EMBEDDING_DIM=1024   # ⚠️ 与 init.sql VECTOR(N) 一致
RECALL_HOPS=2 ; HOP_DECAY=0.7                      # V16 多跳 + 跨跳衰减
DECAY_HALF_LIFE_DAYS=30 ; DECAY_ALPHA=0.3          # V16 时间衰减；半衰期 <=0 关
```

### 4.4 外部依赖
- **Postgres + pgvector**：`docker-compose.yml` 起 `pgvector/pgvector:pg16`，容器 `nano-memory-pg`，端口 `127.0.0.1:5432`
- **mock memory server**：`scripts/mock_memory_server.py` FastAPI + 知识图谱，监听 `127.0.0.1:8765`

---

## 5. 验活 cheatsheet

```bash
# 当前分支
git -C /Users/qshf/my-project/nano_hermes_agent branch --show-current

# DB 容器健康吗 + 知识图谱表状态
docker ps --filter name=nano-memory-pg --format "table {{.Names}}\t{{.Status}}"
docker exec nano-memory-pg psql -U nano -d nano_memory -c "SELECT 'entities' as t, count(*) FROM entities UNION ALL SELECT 'relations', count(*) FROM relations UNION ALL SELECT 'facts', count(*) FROM facts;"

# mock server 健康 + 三个核心端点
curl -s --noproxy '*' http://127.0.0.1:8765/healthz | python -m json.tool
curl -s --noproxy '*' -X POST http://127.0.0.1:8765/retain -H 'Content-Type: application/json' -d '{"content":"User: 我叫小明\nAssistant: 好"}' | python -m json.tool
curl -s --noproxy '*' -X POST http://127.0.0.1:8765/recall -H 'Content-Type: application/json' -d '{"query":"小明用什么语言"}' | python -m json.tool
curl -s --noproxy '*' -X POST http://127.0.0.1:8765/reflect -H 'Content-Type: application/json' -d '{"query":"用户的所有信息"}' | python -m json.tool

# 重建 DB（schema 变了）+ 起 mock server（前台日志）
cd /Users/qshf/my-project/nano_hermes_agent && docker compose down -v && docker compose up -d
cd /Users/qshf/my-project/nano_hermes_agent && \
  .venv/bin/python -m dotenv -f .env run -- .venv/bin/python scripts/mock_memory_server.py

# 跑某档不变量测试（每档 8–13 项）
.venv/bin/python /Users/qshf/my-project/nano_hermes_agent/scripts/test_v<N>_*.py
# 现有：v12_e2e/writer, v13_prefetch, v14_session_switch, v15_compress, v15_1_compress_boundary,
#       v16_batch_decay/leiyu_recall_trace, v17_transport, v18_anthropic, v19_failover,
#       v20_prompt_cache, v21_slash, v21_2_prompt_builder, v21_3_skill, v21_4_tool_result_protocol,
#       v22_streaming, v23_0_delegate, v23_1_batch, v23_2_project_context, v23_3_streaming, v23_4_structured_result

# 看当前装了几个 skill
ls /Users/qshf/my-project/nano_hermes_agent/skills/

# V23.2 — 跨项目跑 agent（在 nano 目录之外启动）
.venv/bin/python /Users/qshf/my-project/nano_hermes_agent/main.py --cwd /Users/qshf/Documents/book
```

文档：`docs/usage-input-and-cancel.md`（V22 输入框按键 / Esc-Enter 多行 / 流式 Ctrl+C 取消）。

---

## 6. 决策日志 / 待办

- 决策：[docs/decisions/README.md](docs/decisions/README.md) 索引；新版本完成后追加 `v<N>.md`
- 维护规则：只增不删；旧选择被推翻 → 保留旧记录 + 新版本日志写"为什么改"
- 待办：[docs/todo.md](docs/todo.md)

**总长硬规则**：本文件只放索引 + 不变区，决策日志 / 待办全部外部化，**控制在 200 行内**。
