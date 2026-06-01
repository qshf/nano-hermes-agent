# nano_hermes_agent — 教学版 AI Agent 迭代之旅

> 从 0 行代码到完整的工具系统 + 长期记忆 + 多 provider 协议解耦 + 流式交互 + 多智能体 + 会话持久化 + 数据飞轮的 30 档迭代。
> 源项目：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级 AI Agent）。

每一档都解决前一档暴露的具体痛点。每个分支是一个**完整可运行**的 checkpoint，
checkout 即可看到当前阶段的全部代码 + 配套测试。

> **关于分支链路**：少数档（如 v21.4、v23.0）没有独立分支，其代码被紧随其后的分支吸收，
> 表格里链接指向承载它的分支并在概念列注明。

---

## 学习路径

### 工具系统（`tools/`）— 从硬编码到插件化

| 分支 | 引入概念 |
|------|---------|
| [`tools/v0.0`](../../tree/tools/v0.0) | if/elif 工具调度 — 字符串硬编码 |
| [`tools/v0.1`](../../tree/tools/v0.1) | 工具注册表 — dict 注册 + 元数据 |
| [`tools/v0.2`](../../tree/tools/v0.2) | OpenAI 兼容 tool calling — function/tool schema |
| [`tools/v0.3`](../../tree/tools/v0.3) | 工具分组（toolsets）— 按场景挂载 |
| [`tools/v0.4`](../../tree/tools/v0.4) | system prompt 构建器 — prompt 拼装 |
| [`tools/v0.5`](../../tree/tools/v0.5) | MCP 协议接入 — 跨进程工具 |

### 记忆系统（`memory/`）— 从文件快照到知识图谱

| 分支 | 引入概念 |
|------|---------|
| [`memory/v0.6`](../../tree/memory/v0.6) | 文件记忆（builtin）— MemoryStore + 冻结快照 |
| [`memory/v0.7`](../../tree/memory/v0.7) | MemoryProvider ABC — 抽象后端接口 |
| [`memory/v0.8`](../../tree/memory/v0.8) | MemoryManager 编排 — 路由 + 围栏 + 隔离 |
| [`memory/v0.9`](../../tree/memory/v0.9) | Agent Loop 生命周期 — prefetch / sync_turn 钩子 |
| [`memory/v0.10`](../../tree/memory/v0.10) | RemoteSemanticProvider — HTTP 边界（mock dict） |
| [`memory/v0.10.1`](../../tree/memory/v0.10.1) | pgvector + OpenAI embedding — 真实向量存储 |
| [`memory/v0.11`](../../tree/memory/v0.11) | 知识图谱记忆（Hindsight 1:1）— 实体/关系/事实 + 多策略检索 |
| [`memory/v0.12`](../../tree/memory/v0.12) | 异步 retain — 后台 writer 线程 + queue + sentinel |
| [`memory/v0.13`](../../tree/memory/v0.13) | 后台 prefetch 预热 — 两阶段消费 + 冷启动 fallback |
| [`memory/v0.14`](../../tree/memory/v0.14) | 会话切换 — `/new` + `/resume` + drain writer |
| [`memory/v0.15`](../../tree/memory/v0.15) | 上下文压缩 — 五阶段流水线 + on_pre_compress 钩子 |
| [`memory/v0.16`](../../tree/memory/v0.16) | retain 批量 + 多跳图遍历 + 时间衰减 |

### Transport 层（`transport/`）— Provider 协议解耦

| 分支 | 引入概念 |
|------|---------|
| [`transport/v0.17`](../../tree/transport/v0.17) | Transport ABC + ChatCompletionsTransport — LLM 调用收敛进抽象边界 |
| [`transport/v0.18`](../../tree/transport/v0.18) | AnthropicTransport + Registry — 第二家 transport 验证 ABC 价值 |
| [`transport/v0.19`](../../tree/transport/v0.19) | TransportChain + 断路器 — 多后端故障切换 + 失败阈值 + 冷却 |
| [`transport/v0.20`](../../tree/transport/v0.20) | Prompt Cache — Anthropic ephemeral system_and_3 + Usage 拆 read/write |

### 交互层（`skill/`）— slash 命令与 Skill

| 分支 | 引入概念 |
|------|---------|
| [`skill/v0.21.1`](../../tree/skill/v0.21.1) | slash 命令注册表 — `/` 命令解析 + 分发 |
| [`skill/v0.21.2`](../../tree/skill/v0.21.2) | PromptBuilder 三段式 — 删模板，为 skill 索引段占位 |
| [`skill/v0.21.3`](../../tree/skill/v0.21.3) | Skill 系统 — progressive disclosure tier1 索引 + tier2 skill_view 工具 |
| [`skill/v0.21.3`](../../tree/skill/v0.21.3) | **v21.4**（同分支 tip）tool result 协议收口 — tool_result/tool_error + dispatch 三层兜底 |

### 流式（`stream/`）— 流式输出与中断

| 分支 | 引入概念 |
|------|---------|
| [`stream/v0.22`](../../tree/stream/v0.22) | stream_call + CancelToken — 增量输出 + Ctrl+C 中断 + failover-before-first-event |

### 多智能体（`delegate/`）— 任务委派与并行

| 分支 | 引入概念 |
|------|---------|
| [`delegate/v0.23.1`](../../tree/delegate/v0.23.1) | **v23.0**（同分支历史）delegate_task + 隔离 child_loop + 工具黑名单 |
| [`delegate/v0.23.1`](../../tree/delegate/v0.23.1) | tasks[] 批量并行 + 工具子集白名单 + ThreadPoolExecutor |
| [`delegate/v0.23.2`](../../tree/delegate/v0.23.2) | 跨项目可用 — `--cwd PATH` + AGENTS.md 注入 + NANO_IGNORE_RULES |
| [`delegate/v0.23.3`](../../tree/delegate/v0.23.3) | 多智能体流式中继 + 父子 cancel — stream 透传到 child + 共享 CancelToken |
| [`delegate/v0.23.4`](../../tree/delegate/v0.23.4) | 多智能体结构化结果 + 成本聚合 — `{"results":[...]}` + 4 维 token + tool_trace |

### 会话持久化（`session/`）— SQLite 状态与真 resume

| 分支 | 引入概念 |
|------|---------|
| [`session/v0.24.0`](../../tree/session/v0.24.0) | SQLite 会话子系统 — sessions + messages 两表 + WAL + 真 resume |
| [`session/v0.24.1`](../../tree/session/v0.24.1) | append-only 写入 + 压缩链 — 无状态游标 + 会话分裂 + 链路折叠 |

### 数据飞轮（`flywheel/`）— 训练样本与离线分析

| 分支 | 引入概念 |
|------|---------|
| [`flywheel/v0.25.0`](../../tree/flywheel/v0.25.0) | trajectory 训练样本导出 — OpenAI messages → ShareGPT + 脱敏 + 子轨迹落盘 |
| [`flywheel/v0.25.1`](../../tree/flywheel/v0.25.1) | insights 离线分析 + 结构化日志 — 读 SQLite 出 token/成本/失败率报表 + 写盘前脱敏 |

---

## 怎么用

```bash
# 选一个想学的版本
git checkout memory/v0.12

# 看当前分支的 CLAUDE.md 了解前置依赖和决策日志
cat CLAUDE.md

# 安装依赖
pip install -e .

# 跑测试
python scripts/test_v12_writer.py
```

每个分支的 `CLAUDE.md` 都包含：
- 该版本引入的核心概念
- 设计决策日志（**选什么 / 没选什么 / 原因**）
- 验活 cheatsheet
- 与源项目的对照路径

---

## 设计原则

1. **每一档解决前一档暴露的具体痛点** — 不是为了 feature 而 feature。
2. **决策都有备选项** — 每个 ABC、每个抽象都对照"如果不抽会怎样"。
3. **真实工程踩坑必记** — 比如 `memory/v0.10.1` 里 `EMBEDDING_DIM` vs `VECTOR(N)` 维度不一致那个 bug。
4. **教学版本的 nano 永远不到 v1.0** — 保留 `v0.X` 是因为这是教学项目，不是生产项目。

---

## 相关文档

- [`docs/system-roadmap.md`](docs/system-roadmap.md) — 系统级演进路线图（建议先读）
- [`docs/memory-system/iteration-plan.md`](docs/memory-system/iteration-plan.md) — 记忆系统迭代规划
- [`docs/Transports-system/iteration-plan.md`](docs/Transports-system/iteration-plan.md) — Transport 层迭代规划

> 这些文档分布在各功能分支的 `docs/` 目录下，main 分支只做导航。
