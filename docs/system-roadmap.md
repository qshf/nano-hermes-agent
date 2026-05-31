# nano_hermes_agent — 子系统实现先后顺序考量

> 本文档解释：在 V0–V20 已经把"工具系统 + 记忆系统 + 传输层系统"三条主线打通后，**剩下哪些子系统值得做**、**为什么这个顺序**、**每档的最小切片是什么**。
>
> 受众：未来的自己 / 协作 LLM（跨会话保持决策一致）。
> 维护规则：每开一个新版本前回来 review；新增子系统插入到对应优先级；落地后把"状态"列改成 ✅。
> 历史：本文档前一版（V16 时代）的预测路线图被实际执行偏离 —— 当时预测 V19 = delegate，实际走成 V17-V20 全部用于 transport 主线。新版本基于 v20 已结束的现实重写。

---

## 0. 已完成的三条主线

| 主线 | 范围 | 终点版本 | 核心抽象 |
|------|------|---------|---------|
| 工具系统 | V0 → V5 | V5 MCP 接入 | `Tool` 注册表 / toolsets / tool calling schema / MCP 跨进程协议 |
| 记忆系统 | V6 → V16 | V16 多跳 + 时间衰减 | `MemoryProvider` ABC / `MemoryManager` 编排 / 围栏 / 知识图谱 / 读写双异步 / 会话切换 / 上下文压缩 |
| 传输层系统 | V17 → V20 | V20 Prompt Cache | `Transport` ABC / ChatCompletions + Anthropic / TransportChain + 断路器 / cache_control 注入 |

**共同设计模式**（已经反复出现，下一阶段可继续复用）：
- ABC + 多实现：`Tool` / `MemoryProvider` / `Transport` 都是接口与实现分离
- 单一集成点：tool registry / MemoryManager / TransportChain 都是 agent loop 的"门面"
- 生命周期钩子：on_turn_start / prefetch / sync_turn / on_session_switch / on_pre_compress
- HTTP 边界 + 服务端独立演化：mock_memory_server 验证了"两端独立演化"
- 错误隔离 + 围栏 + 启动期 fail-fast
- 注册表 + env-driven 路由：transport registry / memory provider registry

---

## 1. 候选子系统全景（源项目对照）

源项目 hermes-agent 中**还没在 nano 里复现的**主要子系统，按教学价值×自包含度排序：

| 候选 | 源项目位置 | 行数 | 一句话定位 | 教学价值 | 自包含度 |
|------|----------|------|-----------|---------|---------|
| **Skill 系统** | `tools/skills_tool.py` + `agent/skill_utils.py` + `agent/skill_commands.py` + `skills/*` | ~2700 (核心) | Markdown 驱动的指令分层加载（Tier 1 索引 + Tier 2/3 全文） | ★★★★ | ★★★★★ |
| **Slash Command 系统** | `cli.py` + `hermes_cli/commands.py` + `tools/slash_confirm.py` | 13253 + 30 个文件 | 人机交互层的命令注册表（vs Tool 系统是 LLM 调用层）；help / dispatch / argparse | ★★★ | ★★★★★ |
| **多智能体（delegate）** | `tools/delegate_tool.py` | 2767 | 父 agent spawn 子 agent，受限 toolset + 独立 context + 并发汇总 | ★★★★★ | ★★★★ |
| **流式 + 中断** | 各 transport 的 stream 路径 + `tools/interrupt.py` | ~1500 | SSE 流式解析 + 中断信号注入 + 流中故障切换 | ★★★★★ | ★★★ |
| **Mixture-of-Agents** | `tools/mixture_of_agents_tool.py` | 1300+ | 多 transport 平行回答 + 聚合器合成 | ★★★ | ★★★ |
| **Approval / Hook** | `tools/approval.py` + `tools/skills_guard.py` + `agent/shell_hooks.py` | 2200+ | pre-tool-call 钩子链 + 危险命令检测 + 三档审批模式 | ★★★ | ★★★★ |
| **Trajectory + Insights（日志 + 训练数据）** | `agent/trajectory.py` + `agent/insights.py` + `agent/redact.py` + `hermes_logging.py` | ~1780 | 对话→训练格式落盘 + 历史聚合统计 + 日志脱敏 + 结构化日志 | ★★★★ | ★★★★ |
| Todo / Clarify 工具 | `tools/todo_tool.py` + `tools/clarify_tool.py` | ~600 | agent 自我管理（todo）+ 反向追问用户（clarify） | ★★ | ★★★★★ |
| Curator 后台编排 | `agent/curator.py` | 1781 | 长期运行后台任务 + 状态机 + 不可变约束 | ★★★ | ★★ |
| Trajectory Compressor | `trajectory_compressor.py` | 1508 | 中间段摘要替换 + 边界保护 + 令牌预算 | ★★★ | ★★★★ |

**已主动放弃的方向**：
- Gateway 多平台分发（Telegram/Discord/Slack）：3000+ 行平台胶水，AI 概念稀薄
- Skills Hub（GitHub 同步）：3261 行，依赖外部基础设施
- Platform Adapter / TUI：渲染层，与 agent 核心逻辑无关
- **Batch Runner / Eval**（`batch_runner.py`，1302 行）：是"批量跑 agent + 检查点恢复 + 轨迹落盘"的编排器，本质是任务编排 + 文件 IO，AI 概念稀薄。它依赖 dataset 格式约定 + multiprocessing，对教学版价值低。如以后要做模型评估再考虑独立开档。
- **Backend abstraction**（`agent/anthropic_adapter.py` / `agent/bedrock_adapter.py` 等，5300+ 行）：与 Transport **分两层**——Transport 关心协议（消息格式、SSE、cache_control，已在 V17-V20 覆盖），Backend 关心**多源凭证 + 原生 SDK 初始化 + provider 怪癖**（如 `sk-ant-api*` 走 `x-api-key`、`sk-ant-oat*` 走 OAuth Bearer、Bedrock 走 AWS IAM 链、Gemini 走 Google OAuth）。**前提是真接入 ≥3 家**才有教学价值；目前 nano 只用 DeepSeek + Qwen DashScope，凭证都是单一 API key，真实需求不存在。等扩到 Bedrock/Gemini 时再独立开档。
- Curator 后台编排：依赖辅助模型 + 状态机持久化，自包含度差

---

## 2. 决策：v21-v23 的排序

### 2.1 三个候选 & 它们各自的痛点起源

进入 v21 之前，最强的三个候选：

| 候选 | 痛点起源 | 教学新概念 |
|------|---------|----------|
| **Skill 系统** | v4 的 `build_system_prompt` 实际只是 5 行 f-string，不是真 builder；agent 缺少"按需加载指令"范式 | 渐进式披露 / 上下文经济学 |
| **流式 + 中断** | v17-v20 的 transport 抽象只用了一半（同步），blocking 调用导致每次响应空屏 5-30s；Ctrl+C 只能杀进程 | SSE 解析 / cancel token / 流中故障切换 |
| **多智能体** | agent 单线程串行；缺少任务分解 + 并行能力；没法体检前 20 档抽象的隔离质量 | 并发池 / 工具白名单 / 子上下文构造 |

### 2.2 为什么 v21 = Skill 系统 + v4 prompt builder + Slash Command 注册表（三合一）

**理由 1：v4 阶段共有三件设计债，主题统一**

v21 不是只做一件事，而是清算 v4 阶段的三件交互层债务：

| 设计债 | 现状 | v21 解法 |
|-------|------|---------|
| prompt builder | 5 行 f-string，名实不符 | 三段式 builder（骨架 + skill 索引 + 工具列表） |
| 指令加载范式 | 一次性全灌 system prompt | progressive disclosure（tier 1 索引 + tier 2 主动 view） |
| slash command | agent.py 主循环 343 行 if-elif 链（[agent.py:192-535](../agent.py#L192-L535)）| 注册表 + 装饰器 + dispatch（与 v0→v1 tool 系统同构） |

三件事共享一个主题："**v4 阶段交互层的设计债清算**"。强行拆成 v21/v21.5/v22 反而割裂叙事。

**理由 2：v4 的设计债**

v4 当时的 `build_system_prompt` 是这样的：

```python
SYSTEM_PROMPT = """You are a helpful coding assistant. ...{tool_list}..."""
def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    return SYSTEM_PROMPT.format(tool_list="\n".join(f"- `{n}`" for n in tool_names))
```

5 行 f-string 替换。CLAUDE.md 进度表把它写成"system prompt 构建器"，**名实严重不符**。"v4 实现不好"是已知事实。

**理由 3：skill 系统正好提供"重做 v4 的真实需求"**

CLAUDE.md 第 4 行写的项目核心叙事是"**每一档解决前一档暴露的具体痛点**"。v4 的痛点是 prompt builder 太薄 —— 而**暴露这个痛点的恰恰是 skill 需求**：要做三段式 prompt（骨架 + skill 索引 + 工具列表），单靠 f-string 替换无法支撑。同理，slash 重构的契机来自 v21-v24 每档都新增 1-3 个命令，主循环 if-elif 链将不可维护。

这是教科书式的"前一档暴露 → 后一档解决"。如果单独开 v4.1 做"干净的 prompt builder"或独立开 v20.5 做"slash 重构"，反而是脱离需求的过度设计。

**理由 4：skill 是教 progressive disclosure 的最干净例子**

源项目 [`tools/skills_tool.py`](file:///Users/qshf/my-project/hermes-agent/tools/skills_tool.py) 文件头注释明确写：

> - skills_list: List skills with metadata (progressive disclosure tier 1)
> - skill_view: Load full skill content (progressive disclosure tier 2-3)

两层加载机制：tier 1 把所有 skill 的 `name + description` 注入 system prompt（一行一个，几十个 skill 也只占几百 token）；tier 2/3 由 agent 推理后**主动调 `skill_view` 工具**按需 fetch 完整内容。

这跟 v15 上下文压缩形成正交：**v15 是"事后省 token"，skill 是"事前不发"**。两者互补，缺一档就少一种核心范式。

**理由 5：v21 是 v22-v24 的硬前置**

- skill 部分：源项目子 agent 最佳实践是 `subagent-driven-development` skill —— **子 agent 拿到精简 skill 索引，自己选要 view 哪个工作流**。先 skill 后多 agent，两档形成连续叙事
- slash 部分：v22 `/cancel`、v23 `/agents`、v24 `/insights` 都依赖一个干净的 slash registry；不先做后续每档都要在主循环加 if-elif 分支

### 2.3 为什么不先做流式 + 中断

诚实说：**第一版排序里我把流式排在 v23，理由是错的**。被反问后重审，流式其实是更强的 v21 候选，但最终仍选 skill，理由是：

| 维度 | Skill 系统 | 流式 + 中断 |
|------|----------|-----------|
| 痛点显然度 | 中（要造场景才感受 token 浪费） | **高（开机就有，每次响应空屏 5-30s）** |
| 与 v4 的关联 | **强（直接修复 v4 设计债）** | 无 |
| 是 v22 前置吗 | 强（subagent-driven 范式） | 强（中断信号机制） |
| 难度曲线 | 中（v20 后稍降） | 高（v20 后再升） |
| Transport 弧线 | 不影响 | **承接 v17-v20，做完才闭环** |

**两条路线都成立**。最终选 skill 的关键理由是 **v4 设计债** —— 这是无法被流式覆盖的独立痛点。流式无论何时做，都不会顺手修好 v4。把"修 v4"延后会让它一直挂着。

但 **流式必须排 v22**（不是 v23），因为它是多 agent 的硬前置：
- 多 agent 必须有中断 —— 不然跑飞的子 agent 没法 kill
- 多 agent 的并发输出最自然的呈现方式就是流式
- 后做流式 = 给 v22 多 agent 打补丁，不如先做

### 2.4 路线图总览

```
[已完成 v0-v20]
  V0-V5  Tool 系统
  V6-V16 Memory 系统
  V17-V20 Transport 系统
        ↓
v21  Skill 系统 + v4 重构 + Slash Command 重构  ← 上下文经济学 / 三件 v4 设计债一次清
        ↓
v22  流式输出 + 中断                         ← Transport 弧线收尾 / 多 agent 前置
        ↓
v23  多智能体 delegate                       ← 并发 + 隔离 + 抽象体检
        ↓
v24  Trajectory + Insights（日志/训练数据） ← 数据飞轮起点
        ↓
v25  Mixture-of-Agents（可选）              ← 平面并行（依赖 v23）
        ↓
v26  Approval / Hook 系统（可选收尾）       ← 安全围栏
        ↓
v27  Todo / Clarify（可选小尾巴）           ← 协作工具
```

**承诺范围**：实际只承诺到 v24。做完后 nano 已经是"能渐进加载、能流式中断、能并行、能落盘训练数据"的真 agent，并具备数据飞轮起点。v25+ 按精力和兴趣追加。

> **路线偏离记录（2026-05-31，场景 C）**：原计划 v24 = "Trajectory + Insights"。
> 实际做 v24 时发现 **trajectory 落盘的前置是会话本身能落盘** —— 而 v14 标称的
> 会话持久化是假的（只切 memory bank key，从没存过 messages，`/resume` 拿不回
> 历史，进程退出对话蒸发）。所以把 v24 拆成两步先补这个债：
> - **v24.0 会话状态持久化**（已完成）：`agent/session_store.py`，SQLite sessions +
>   messages 两表 / WAL / 全量删重插 / 真 resume / `/sessions`。见
>   [docs/decisions/v24.0.md](decisions/v24.0.md)。
> - **v24.1**（下一档）：append-only 游标增量写 + 压缩链（会话分裂 + parent 串链 +
>   resume 重定向到 tip）。
> - **v24.2+ Trajectory + Insights**：复用 v24.x 的 SQLite 会话后端落训练数据。
>
> 偏离理由：原路线图把"会话持久化"默认当成 v14 已交付，实际是技术债。trajectory
> 直接建在假持久化上等于沙上建塔。先补债、再上飞轮，依赖顺序才对。

### 2.5 排序依据（四条原则）

1. **痛点驱动**：每档必须修一个具体痛点。v21 修 v4 设计债，v22 修 blocking 体感，v23 修单线程局限。
2. **依赖优先**：v23 必须在 v22 之后（中断是多 agent 前置）；v24 必须在 v23 之后（复用并发基建）。
3. **叙事连续**：v17-v22 是一整段"传输与运行时"弧线（抽象 → 多家 → 故障切换 → cache → 流式中断）；v22-v24 是"agent 数量从 1 到 N"弧线。
4. **难度避连击**：v20 cache 控制是硬档，v21 skill 中等（缓冲）→ v22 流式高 → v23 多 agent 高 → v24 中等。避免连续两档高难度。

### 2.6 如果改主意先做 v22 流式会怎样？

可以，代价是：
- v21 skill + v4 重构会被推到 v23 之后做，但那时已经做完多 agent，**v4 的 prompt builder 在多 agent 子上下文构造里已经被绕过去**，单独修 v4 失去原本"被 skill 需求逼出来"的叙事张力
- 整个 v17-v23 都是 transport / runtime 主题，连续 7 档不换轴，叙事疲劳
- skill 系统永远找不到一个"真实需求逼出 prompt builder 重写"的契机

**结论**：可以做，但要主动接受 v4 设计债延期偿还的成本。

---

## 3. 各档最小切片（启动时再细化）

### V21 Skill 系统 + v4 prompt builder 重构 + Slash Command 注册表重构

**核心问题**（三件 v4 阶段的设计债，一次性还清）：
- v4 的 `build_system_prompt` 是 5 行 f-string，无分层、无可扩展点
- agent 缺少"按需加载指令"范式 —— 所有指令都在 system prompt 一次性灌入
- agent.py 主循环里 [agent.py:192-535](../agent.py#L192-L535) 是 343 行的 slash command if-elif 链，每加一个新命令都要改主循环 —— v0→v1 的"if/elif → 注册表"模式在 slash 这一层从未做过

**为什么三件事合并成一档**：

主题统一是"v4 阶段交互层债务清算"。更关键的是，**v22-v24 每一档都至少新增 1-3 个 slash command**：
- v21 自身：`/skill list` / `/skill view <name>` / `/skill reload`
- v22 流式：`/cancel` / `/stream on|off`
- v23 多 agent：`/agents`（看活跃子 agent）/ `/cancel <id>`
- v24 训练数据：`/insights [--days 30]` / `/trajectory list`

不先做 slash 注册表，主循环会从当前 343 行膨胀到 600+ 行，后续每档都背着这个债。

**最小可教学切片**：

**A. Slash Command 注册表**（200-300 行）
1. 新增 `cli/registry.py`：
   - `SlashCommand` ABC（`name` / `description` / `usage` / `execute(args, ctx)`）
   - `@command("/skill")` 装饰器自动注册
   - `dispatch(input_line, ctx)`：分词 + 路由 + 错误兜底
2. 新增 `cli/commands/`：每个命令一个文件，从 agent.py 抽出
   - `memory.py` / `mcp.py` / `plugin.py` / `session.py` / `transport.py` / `compress.py` / `tools.py`
3. 重写 `agent.py` 主循环：
   - slash 分支瘦到 3 行：`if user_input.startswith("/"): cli.dispatch(user_input, ctx); continue`
   - `ctx` 注入 manager / chain / messages 等全局对象
4. `/help` 自动从 registry 渲染所有命令 + 描述

**B. v4 prompt builder 重构**（150-200 行）
1. 三段式 builder：骨架 prompt + skill 索引段 + 工具列表段
2. 骨架可注入（角色 / 风格 / 安全约束分段）
3. 每段独立可测试

**C. Skill 系统**（400-500 行）
1. 新增 `skills/` 目录，3-5 个示例 skill（每个一个子目录 + `SKILL.md`）
2. 新增 `agent/skill_loader.py`：
   - `parse_frontmatter`：解析 YAML frontmatter（`name` / `description` / `platforms`）
   - `scan_skills`：扫描目录 + 平台过滤 + 注册表
   - `build_skill_index`：tier 1 索引（仅 name + description）
3. 新增 `tools/skill_view_tool.py`：tier 2 工具，agent 主动 fetch 完整 markdown
4. `/skill list` / `/skill view <name>` / `/skill reload` 三个 slash 命令
5. 决策日志：v4 设计债演进逻辑 + slash 重构契机

**验证**：
- `agent.py` 主循环 343 → ~30 行（slash 部分）
- `/help` 自动列出所有命令
- 启动时 system prompt 中可见 skill 索引
- agent 在适当场景能调用 `skill_view` 加载详情
- 关 skill（env 开关）后退化到类 v4 行为，回归测试通过

**简化掉的**（vs 源项目）：
- 不做 GitHub 同步 / Skills Hub
- 不做 skills_guard 安全检测 / provenance / usage 统计
- 不做条件激活（`requires_toolsets`）
- 不做 slash 命令的 argparse 完整解析（手写简化版分词即可）
- 不做 slash 命令的自动补全 / 历史

**预估规模**：800-1100 行核心代码 + 3-5 个 markdown 示例

---

### V21.4 工具结果协议收口（已完成）

**核心问题**：
- V21.3 后审视工具体系，发现 6 个工具各自重复 `json.dumps({...}, ensure_ascii=False)` 拼装结果，字段名虽对齐源项目（`error` / `output` / `content`）但缺统一入口
- `tools/mcp_client.py:160` 直接 `return "\n".join(parts)` 是裸字符串，破坏"工具结果都是合法 JSON 字符串"的协议假设
- `registry.dispatch` 没有最终防线：handler 抛异常会冒泡到 agent loop，返回非 str / 非 JSON 也无人兜底

**最小切片**（已落地）：
1. 新增 `tools/result.py` — `tool_result(data=None, **kwargs)` / `tool_error(msg, **extra)`，仿源项目 `tools/registry.py:537-548`
2. 6 个工具改用辅助函数（skill_view / read_file / terminal / docker_exec / async_demo / mcp_client）
3. `mcp_client._make_handler` 把 server text content 拼接结果包成 `{"output": "..."}`
4. `registry.dispatch` 加最终防线：handler 抛异常 → `tool_error(...)`；返回非 str → `tool_result(output=str(...))`；返回非合法 JSON → `tool_result(output=<原文>)`
5. `scripts/test_v21_4_tool_result_protocol.py` — 10 项不变量测试覆盖辅助函数 + 真实工具协议 + dispatch 兜底三类

**主动留到后期的"大输出沙箱持久化"**（不在 V21.4 范围）：

源项目 `tools/tool_result_storage.py:122-176` 的 `maybe_persist_tool_result` 提供更彻底的解法 —— 工具结果超过阈值时，把完整内容落盘到 sandbox（`/tmp/hermes-results/{tool_use_id}.txt`），返回给模型的是 `<persisted-output>` 标签包的预览 + 文件路径，模型按需用 `read_file` 再展开。

**为什么先不做**：
- 当前 nano `skills/` 下 skill 文件都不大（最长 ~80 行），没出现 context 撑爆问题
- 持久化机制涉及沙箱目录管理、`tool_use_id` 关联、清理策略（TTL / 容量上限）三件独立设计，跟"协议统一"耦合度低
- 与"每档解决一个具体痛点"的 nano 节奏冲突 —— 强行塞进 V21.4 会让叙事杂糅

**触发条件**：当出现以下任一情况时独立开档（暂记 V21.5 候选）：
- 单次工具结果（read_file / skill_view / terminal stdout）超过 8 KB 在 messages 里反复留存
- prompt cache 命中率因工具结果体积大而显著下降
- 出现需要"工具产出大文件 → 后续 turn 引用"的场景（数据导出 / 大段日志检索）

**预估规模**：辅助 + 重构 + 测试 = ~150 行变更（已完成）

---

### V22 流式输出 + 中断（已完成）

**核心问题**：
- 前 21 档全是 blocking 调用，体感差
- Transport ABC 只用了同步那半，未真正闭环
- Ctrl+C 只能杀进程，不能优雅打断流

**最小切片**（已落地）：
1. 新增 `transports/streaming.py`（~150 行）— `StreamEvent` / `CancelToken`（threading.Event）/ `StreamCancelled` / `StreamIterator`
2. `ProviderTransport` ABC 增加 `stream_call(client, cancel_token, **kwargs)`，默认实现 = `call()` + 1 个 text_delta + done（让未重写流式的 transport 也不至于让 agent loop 崩）
3. `ChatCompletionsTransport.stream_call`：`stream=True + stream_options={"include_usage": True}`；
   - 文本 / reasoning concat 实时 emit
   - tool_call name 用赋值（防 MiniMax M2.7 重发污染）/ arguments 用 += （spec 分片）
   - usage 在最终 `choices=[]` 帧抓取
4. `AnthropicTransport.stream_call`：`client.messages.stream()` 上下文管理器
   - `content_block_start` 中 `tool_use` block → emit started
   - `content_block_delta` 中 `text_delta` / `thinking_delta` → emit delta
   - 流尾 `stream.get_final_message()` 复用 `normalize_response`
5. `TransportChain.stream_call`：**首帧前可切家**，已 yield 后失败禁止切家（避免 token 重发）；`StreamCancelled` 透传不计为失败；done 帧累计 cache 统计与同步一致
6. `cli/commands/stream.py` — `/stream on|off` 切换；`AgentCtx.stream_enabled` / `cancel_token` 字段
7. `main.py` 改造：`signal.signal(SIGINT, ...)` 用 `streaming_active` 旗标分流 prompt vs 流式期间；`_stream_one_turn` 辅助函数 emit → stdout 实时打印；`chain.call → ctx.stream_enabled` 三态分发
8. `scripts/test_v22_streaming.py` — 13 项不变量（CancelToken / SSE 增量 / tool_call 累积 / 默认假流式 / chain failover-before-first-event）

**验证**（已通过）：
- 13/13 V22 不变量 + 105 项 V17–V21 旧不变量零回归
- 等待真跑验证：DeepSeek 流式 Ctrl+C 当帧停止 / `/stream off` vs `on` usage 一致 / DashScope thinking_delta 到达
  → 写入 `docs/todo.md` 的 V22 待办块，留给 V23 启动前手测

**简化掉的**（vs 源项目 ~1500 行）：
- 不做流中切换 transport（首帧后失败直接抛，不重连）
- 不做 SSE 重连 / 断点续传
- 不做 stream diagnostic 计数器（chunks / bytes / first_chunk_at）
- 不做 partial tool args 修复（``_repair_tool_call_arguments``）
- 不做 reasoning box 实时回显（仅打个 `[think] ...` 占位）
- 不做工具结果的流式回灌

**实际规模**：~600 行核心代码 + ~400 行测试（roadmap 预估 500–700 行，吻合）

---

### V23 多智能体（delegate_task）

**核心问题**：
- agent 单线程串行，复杂任务无法分解
- 缺少父子隔离的教学示例
- 前 22 档抽象的隔离质量未被检验

**最小可教学切片**：
1. 新增 `tools/delegate_tool.py`：`delegate_task(goal, toolset, context)` schema
2. spawn 子 AIAgent：
   - 构造 fresh `messages`（不继承父对话历史）
   - 注入 goal 为子 system prompt
   - 受限 toolset（DELEGATE_BLOCKED_TOOLS = `{delegate_task, memory_*, send_message}`）
   - 子 agent 复用 V17-V20 transport（chain 可继承也可独立）
3. 并发执行：
   - ThreadPoolExecutor，max_concurrent_children=3
   - 每个子 agent 独立 task_id
4. 结果聚合：
   - 子 agent 跑到自然终止或超时（最多 N 步 tool loop）
   - final assistant text 作为 summary 返回父
   - 父只看到一个 tool_call + summary，看不到子的中间步
5. 与 V22 流式的协同：
   - 父在等待子 agent 时显示进度条
   - 子 agent 的流式输出通过 callback 转发到父（可选）
6. 与 V21 skill 的协同：
   - 子 agent 收到精简 skill 索引（仅相关分类）
   - 复刻源项目 `subagent-driven-development` 范式

**验证**：
- 单元：子 agent messages 不含父对话历史
- 集成：父让 3 个子并行各做一件事（list_dir / read_file / grep），父 context 只看到 1 个 delegate_task tool 的返回
- 隔离：子 agent 在 builtin memory 写入不影响父的 memory bank
- 中断：v22 cancel token 在父侧触发时，所有子 agent 同步 cancel

**简化掉的**（vs 源项目 2767 行）：
- 不做 orchestrator 角色（只 leaf，不递归）
- 不做 max_spawn_depth 追踪
- 不做暂停/恢复
- 不做 TUI 观察层
- 不做 auto-approval
- 不做活跃 subagent 全局注册表

**预估规模**：400-600 行

---

### V24 Trajectory + Insights（日志追踪 + 训练数据）

**核心问题**：
- 前 23 档跑过的对话全部"用完即抛"，无法回放、无法分析、无法转训练数据
- 缺少跨会话的统计视图（token / 成本 / tool 频次 / 失败率）
- 日志可能泄漏 API key（每次调用 transport 都带 Authorization 头）

**为什么放在 v23 之后**：
- 必须先有 v22 流式（capture 增量帧）和 v23 多 agent（每个子 agent 独立 trajectory），收集层才完整
- 一旦做完，**v25+ 的所有实验都自动有数据落盘** —— 形成持续迭代的数据飞轮
- 如果先于 v22/v23 做，会出现"v22/v23 上线后 trajectory 格式要回头改"的回炉

**最小可教学切片**：
1. 新增 `agent/trajectory.py`（~150 行）：
   - `TrajectoryRecorder`：每轮对话挂钩 agent loop，记录 messages + tool_calls + usage
   - `to_training_format`：转 `{from: "human"/"gpt"/"function_call"/"observation", value: ...}` pair list（SFT 标准格式）
   - `<think>` / `<REASONING_SCRATCHPAD>` 标签处理
   - 落盘到 `trajectories/<session_id>/<turn_id>.jsonl`
2. 新增 `agent/redact.py`（~120 行）：
   - 正则脱敏：`sk-*` / `ghp_*` / `Bearer *` / OAuth code
   - 短 token (<18 字符) 全掩码；长 token 保留首 6 末 4
   - 敏感 query 参数名单（`access_token` / `refresh_token` / `api_key`）
   - 在 transport 层和 logger 层都挂钩
3. 新增 `agent/insights.py`（~250 行）：
   - 从 SQLite（v14 引入的会话表）跨会话聚合
   - 报表：累计 token / 估算成本 / tool 调用 top-N / 失败率 / 平均轮长
   - `format_terminal()`：CLI 命令 `/insights [--days 30]` 输出报表
4. 新增 `hermes_logging.py` 简化版（~80 行）：
   - 结构化 JSON 日志（每条带 session_id / turn_id / transport_name）
   - 日志文件按天滚动，自动调用 redact
   - `LOG_LEVEL` env 控制
5. 决策日志：trajectory 格式选 `from-value` vs `messages`、redact 的脱敏强度（短 vs 长 token）

**验证**：
- 跑一轮 5 turn 对话，`trajectories/` 目录出现 5 个 jsonl 文件，格式可直接喂 transformers SFT
- `cat trajectory.jsonl | grep -E "sk-[a-z0-9]{40}"` 应该零命中
- `/insights --days 7` 显示总 token / 总成本 / top tools
- v22 流式 + v23 多 agent 跑完，子 agent trajectory 独立成文件，父 agent trajectory 含 delegate_task 的 summary 但不含子的中间步

**简化掉的**（vs 源项目 1780 行）：
- 不做 SQLite 持久化（用 jsonl 文件存）—— v14 的 SQLite 已够用
- 不做 multi-platform breakdown（gateway 没做）
- 不做 cost 估算的多家 pricing 表（hardcode 当前用的 deepseek + qwen 价格即可）
- 不做基于 LLM 的总结/分析
- redact 只做 ~10 条最常见 pattern，不做完整的 50+ 条

**预估规模**：500-700 行核心代码

**与已有版本的协同**：
- v9 Agent Loop 钩子：trajectory recorder 挂 `on_turn_end`
- v14 会话切换：`on_session_switch` 时 flush 当前 trajectory + 新建文件
- v15 上下文压缩：被压缩前的对话进 trajectory（不丢失训练样本）
- v17-v20 Transport：每个 transport call 经 redact 后写日志
- v22 流式：流式增量帧累积成完整 turn 后再落盘
- v23 多 agent：父子 trajectory 独立 + cross-link（父记录 child_trajectory_path）

---

### V25-V27 草稿（不承诺）

| 版本 | 主题 | 关键点 | 规模 |
|------|------|-------|------|
| V25 | Mixture-of-Agents | N 个 transport 平行回答 + aggregator 合成；复用 V23 ThreadPoolExecutor | 300-400 行 |
| V26 | Approval / Hook | pre-tool-call 钩子链 + 危险命令模式；plan/yolo/step 三档审批 | 400-500 行 |
| V27 | Todo / Clarify | agent 自我管理 + 反向追问；单文件双 tool | 200-300 行 |

---

## 4. Memory 系统次要档（已主动收敛，不再开档）

V11–V16 已经把 Hindsight 的核心生产能力 1:1 落地。剩余打磨项不再单独开档：

| 候选 | 触发条件 | 归并去向 |
|------|---------|---------|
| 实体消歧 + 别名合并 | V23 多 agent + 多用户场景出现重名 | 合并进 V23 的"多租户记忆增强"小节 |
| Bank 模板渲染 | V23 多 agent 需要 `{user_id}_{platform}` 隔离 | 同上 |
| Plugin 发现机制 | 始终未触发 | 暂搁置 |
| 注入扫描 | 出现安全事件 | 合并进 V25 Approval 系统 |
| Schema 迁移工具 | DB 字段变更 ≥ 2 次 | 暂用 `docker compose down -v` |
| 流式围栏 scrubber | V22 流式后 | 合并进 V22 |

---

## 5. 维护规则

1. **顺序变更必须留档**：将来若改路线（例如把 V22 流式提前到 V21），把决策理由追加到第 2.6 节并标注日期。
2. **新子系统插入位置**：候选表（第 1 节）按教学价值×自包含度排序；新增方向插入对应位置。
3. **状态字段同步**：每完成一档，在 CLAUDE.md 进度表打 ✅，并把切片细化记录写入 `docs/decisions/v<N>.md`。
4. **跨文档一致**：本文档、CLAUDE.md 进度表、`docs/decisions/README.md` 三处的版本列表必须保持一致。
5. **决策日志只增不删**：如发现旧选择被新版本推翻，保留旧记录 + 在新版本日志里写"为什么改"。
