# nano_hermes_agent — 教学版 AI Agent 迭代之旅

> 从 0 行代码到完整的工具系统 + 长期记忆 + 多 provider 协议解耦的 19 档迭代。
> 源项目：[hermes-agent](https://github.com/qshf/hermes-agent)（生产级 AI Agent）。

每一档都解决前一档暴露的具体痛点。每个分支是一个**完整可运行**的 checkpoint，
checkout 即可看到当前阶段的全部代码 + 配套测试。

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
