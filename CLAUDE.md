# nano_hermes_agent — 项目上下文 primer

> **新会话起手必读。** 跨目录、跨会话保持上下文一致。每完成一档必须更新。

---

## 1. TL;DR

- **项目定位**：教学版 AI Agent，从零迭代演进到挂载长期记忆 + 多智能体 + 跨项目可用。
- **源项目**：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级，含 gateway / 多模型后端 / SQLite 会话 / 多终端环境 / 插件系统）。
- **当前阶段**：v27.1 — 外部 Voice Orchestrator：v27.0 `ProgressSupervisor` 作为废弃实验保留决策记录；主线改为 host 只发送 bounded `TurnEventEnvelope` + `RuntimePhaseLease`，外部 voice-orchestrator 决定是否播、播几次、何时播、怎么说并调用 `nano_voice_kit`。主智能体不调用 `nano-voice-say`，也不在宿主侧维护工具价值 / cooldown / phrase LLM。详见 [docs/decisions/v27.1.md](docs/decisions/v27.1.md) 与废弃记录 [docs/decisions/v27.0.md](docs/decisions/v27.0.md)。配套独立包 `nano_voice_kit`（平级目录，见根 CLAUDE 记忆）。
- **演进主轴**：内存（v6→v16）→ transport（v17→v20）→ 交互层（v21.x）→ 流式（v22）→ 多智能体（v23.x）→ 会话持久化（v24.x）→ 数据飞轮（v25.x）→ skill 纵深（v26.x）→ observability / voice supervisor（v27.x）。

---

## 2. 路径与仓库

| 角色 | 绝对路径 | git remote |
|------|---------|-----------|
| 源项目 | `/Users/qshf/my-project/hermes-agent` | `https://github.com/qshf/hermes-agent` |
| nano | `/Users/qshf/my-project/nano_hermes_agent` | `git@github.com:qshf/nano-hermes-agent.git` |
| 当前分支 | `skill/v0.26.5`（v27.1 在该分支上完成；如正式切线可另建 `voice/v0.27.1`） | — |

**跨目录硬约束**：源项目和 nano 不在同一目录。"对照源项目读 X 文件"的操作必须用源项目绝对路径，例如 `/Users/qshf/my-project/hermes-agent/plugins/memory/hindsight/__init__.py`。

---

## 3. 进度与下一步

**完整 30 档进度表（版本 / 标题 / 关键词 / 引入概念）见 [docs/decisions/README.md](docs/decisions/README.md)，每档细节看对应 `v<N>.md`。** 本节只留规划。

**下一档候选**：v27.3 voice orchestrator 服务端深化（ContextExtractor / SpeechPolicy / PhrasePlanner / VoiceDispatcher + decision log / 回放）+ FR-5 全链路人耳验收 / pricing 多家对账（pricing_version + actual_cost）/ insights 扩展（platform/skill breakdown + 活动模式）/ v15.2 prefill retry / v23.5 嵌套 delegate（role: orchestrator + max_spawn_depth）/ v24.2 会话级锁修 last-write-wins / FTS5 全文检索。

**已规划档组**：**v26 skill 子系统纵深补强** — ✅ v26.0 bundled 资源发现 + tier 3 读取 + 路径沙箱 / ✅ v26.1 可用性门控 / ✅ v26.2 安全 token 替换 / ✅ v26.3 行为指令注入 / ✅ v26.4 代码级语音心跳 / ✅ v26.5 事件驱动语音进度服务实验废弃。**v27 voice / observability 子系统** — ✅ v27.0 LLM ProgressSupervisor MVP 废弃实验 / ✅ v27.1 外部 Voice Orchestrator host-side（bounded envelope + runtime phase lease + 主智能体零语音工具调用）/ ✅ v27.2 多服务焦点轮播 nano 侧（FR-3 假搜索服务 + FR-4 `NANO_MCP_SERVERS` 挂 MCP；FocusRouter/source_label 在 nano_voice_kit FR-1/FR-2）；后续 v27.3 做服务端深化与 FR-5 联调验收。

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

# V27.1 外部 Voice Orchestrator（host 只发送事实；服务端自行调 nano_voice_kit）
VOICE_ORCHESTRATOR_ENABLED=0
VOICE_ORCHESTRATOR_URL=http://127.0.0.1:8766/v1/turn-events
VOICE_ORCHESTRATOR_TIMEOUT_SECONDS=0.5 ; VOICE_ORCHESTRATOR_QUEUE_SIZE=128
VOICE_ORCHESTRATOR_STREAM_ONLY=1
VOICE_ORCHESTRATOR_MAX_MESSAGE_PREVIEWS=4 ; VOICE_ORCHESTRATOR_MAX_MESSAGE_CHARS=800
VOICE_ORCHESTRATOR_MAX_TOOL_RESULT_CHARS=1200
VOICE_ORCHESTRATOR_SEND_MESSAGE_PREVIEW=1 ; VOICE_ORCHESTRATOR_SEND_TOOL_PREVIEW=1

# V27.2 外部 MCP 服务挂载（FR-4；控制流走 MCP，观测流由服务自报 envelope）
NANO_MCP_SERVERS=search=python:/Users/qshf/my-project/nano_hermes_agent/fake_search_service.py
# 形如 name=command:arg，分号隔多条；connect 后 registry 自动注册 mcp_<name>_<tool>
# 假搜索服务（FR-3）自身读：VOICE_ORCHESTRATOR_URL / VOICE_ORCHESTRATOR_TIMEOUT_SECONDS

# V23.2 项目上下文（详见 v23.2 决策日志）
NANO_IGNORE_RULES=0   # 1 时跳过 nano-hermes-agent.md / AGENTS.md 注入

# V25.1 结构化日志 + insights
LOG_FILE=logs/agent.log   # :none: 关闭文件日志（只 stderr）；滚动 5MB×3
LOG_LEVEL=INFO            # DEBUG / INFO / WARNING / ERROR
# /insights [--days N] 读 v24 SQLite 出报表（不依赖 trajectory）；/trajectory list 列样本文件

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
#       v22_streaming, v23_0_delegate, v23_1_batch, v23_2_project_context, v23_3_streaming, v23_4_structured_result,
#       v24_0_session_store, v24_1_compaction_chain, v26_0_skill_resources, v26_1_availability, v26_2_token_subst,
#       v26_3_inject_directive, v26_4_voice_heartbeat, v27_1_voice_orchestrator, v27_2_mcp_search

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
