# 多智能体系统（V23）— 迭代拆分计划

> 目标：让 nano agent 从 "单线程串行响应" 演进到 "父 agent 通过工具调用 spawn 多个隔离的子 agent，并行完成子任务再聚合回传"。
> 规划范围：**V23.0–V23.3 已承诺**，V23.4 标注为可选追加。
>
> 上一级：[docs/system-roadmap.md](../system-roadmap.md) 的"V23 多智能体（delegate_task）"小节给出了主线定位（"agent 数量从 1 到 N" 弧线的起点）和"为什么排在 V22 流式之后"的依据。本文档是该小节的展开。
>
> 前置版本：V22（流式 + `CancelToken` + `StreamCancelled`）— `CancelToken` 用 `threading.Event` 就是为多 agent 跨线程共享父 token 准备的；本文档 V23.2 将兑现这一前置假设。
>
> 源项目对照：[hermes-agent/tools/delegate_tool.py](https://github.com/qshf/hermes-agent/blob/main/tools/delegate_tool.py)（2767 行）+ [hermes-agent/tools/mixture_of_agents_tool.py](https://github.com/qshf/hermes-agent/blob/main/tools/mixture_of_agents_tool.py)（541 行，独立工具，已在 roadmap 排到 V25）。本文档行号引用以源项目 git 当前 main HEAD 为准。

---

## 0. 源项目概述

源项目的多智能体系统是 **同进程、线程池驱动的子 agent 架构**：

- **入口**：`delegate_task` 是一个普通 LLM 工具（不是 slash 命令）。父 agent 在 tool calling 阶段决定要不要委派。
- **运行模型**：父 agent 主线程构建 N 个子 `AIAgent` 实例（同进程同 Python 解释器），通过 `ThreadPoolExecutor` 并行运行（`tools/delegate_tool.py:2031-2193`）。
- **隔离机制**：每个子 agent 有：
  - 独立 `task_id`（终端会话、文件操作缓存隔离）
  - 全新 `messages` 列表（不继承父对话历史）
  - 聚焦的子 system prompt（仅包含 goal + context，不包含父的完整指令）
  - 工具黑名单（`delegate_task / clarify / memory / send_message / execute_code` 永远拿不到，避免无限递归 / 越权）+ 与父工具集的交集（白名单）
- **并发 + 中断**：`ThreadPoolExecutor(max_concurrent_children)`；父中断时设 `child._interrupt_requested = True`，子在工具调用边界轮询。
- **结果回传**：结构化字典（包含 `summary` / `status` / `tokens` / `tool_trace` / `duration_seconds` / `exit_reason` / `files_read` / `files_written`），由 `delegate_task` 工具序列化为 JSON 字符串塞回父的 tool_result。
- **成本聚合**：子 token usage 折叠回父 `session_estimated_cost_usd`，让父侧 `/insights`（V24 候选）能看到完整成本。
- **嵌套**：默认 `max_spawn_depth=1`（平坦结构）。`role="orchestrator"` 时子可再 delegate；`role="leaf"`（默认）时子拿不到 `delegate_task` 工具。
- **流式中继**：子 agent 的工具调用进度通过 `tool_progress_callback` 中继到父显示层（CLI spinner / gateway SSE）。

**与其他子系统的交互**：
- 子 agent 复用父的 transport chain（V17–V20）—— 故障切换、cache 控制透明继承。
- 子 agent 复用父的 streaming 基建（V22）—— `CancelToken` 跨父子共享是关键。
- 子 agent 不复用父的 memory bank —— 默认黑名单，避免子写脏父的知识图谱。
- 子 agent 收到精简版 skill 索引（V21.3）—— 仅与 goal 相关的 skill 暴露。

---

## 1. 版本规划总览

| 版本 | 标题 | 解决的核心痛点 | 引入的核心概念 | 对应源项目 |
|------|------|---------------|---------------|-----------|
| **V23.0** | 单任务 delegate（最小可用版） | V22 之前 agent 单线程串行；缺少父子隔离的最小教学示例 | `delegate_task` 工具 / 子 `AIAgent` 同步运行 / 工具黑名单 / 隔离 messages | `tools/delegate_tool.py:2626-2743`（schema）+ `865-1158`（构建子）+ `1305-1593`（运行）的最简骨架 |
| **V23.1** | 批量并行 + 工具子集白名单 | V23.0 一次只能 spawn 一个子；多任务相互独立时仍串行；工具集"全继承"过宽 | `tasks: []` 批量 schema / `ThreadPoolExecutor` + `max_concurrent_children` / 单批分支 / `toolsets` 字段（与父白名单交集） | `tools/delegate_tool.py:2071-2193`（批量分发）+ `940-963`（工具交集） |
| **V23.2** | 流式中继 + 中断传播 | V23.1 子跑飞父无法 kill；父等待时 UI 哑；父按 Ctrl+C 只能整体退出 | 父 `CancelToken` → 子 `CancelToken` 桥接 / 子 stream event 经 callback 中继到父显示 / 子 `StreamCancelled` 翻译为 `status="interrupted"` | `tools/delegate_tool.py:678-862`（progress callback）+ `2104-2139, 1500-1507`（中断传播） |
| **V23.3** | 结构化结果 + 成本聚合 | V23.2 父只看到子的 final summary 字符串，黑盒；token / 工具调用次数 / 失败原因都丢了 | 结构化 JSON 返回（`tokens` / `tool_trace` / `duration_seconds` / `exit_reason` / `files_*`）/ 父 `session_estimated_cost_usd` 累加 / `status` 五态枚举 | `tools/delegate_tool.py:1668-1800`（结果 dict）+ `2231-2279`（成本聚合） |
| V23.4（可选） | 嵌套 delegate + 深度限制 | V23.3 仍是扁平 1 层；需要分两阶段分解的复杂任务（如"先调研再实施"）做不了 | `role: "leaf" \| "orchestrator"` / `max_spawn_depth` / 深度溢出强制降级到 leaf | `tools/delegate_tool.py:899-908`（角色解析）+ `905-908, 389-424`（深度检查） |

**承诺范围**：V23.0–V23.3，对应 nano "agent 数量从 1 到 N + 隔离 + 并发 + 可观测" 的完整闭环。V23.4 仅在 V24 trajectory 启动前若有空档时追加；优先级低于 V24，因为：

- V24 trajectory 是数据飞轮起点，落地越早后续每档实验都自动有训练样本
- V23.4 只引入"递归 + 深度计数"两个概念，相对增量小
- 真正的多层规划场景在 nano 教学版里凑不出（源项目里 orchestrator 多用于代码库改造这类大任务，nano 暂无对应场景）

**非目标**（明确放进 V25+ 或主动放弃）：

- **Mixture-of-Agents**：源项目 `tools/mixture_of_agents_tool.py` 是独立工具（不是 delegate 的子模式），roadmap 已排到 V25
- **心跳机制**（防网关超时）：源项目 `tools/delegate_tool.py:1353-1423`；nano 没有 gateway 中间层，子 agent 直跑 transport，不需要
- **ACP 传输覆盖**：源项目用 ACP 协议跨进程跑子 agent；nano 同进程线程池，不引入
- **凭证覆盖**（`delegation.provider/api_key`）：nano 只有 DeepSeek + Qwen 两家，全局 env 已够
- **暂停 / 恢复**：源项目支持把跑到一半的子 agent 序列化暂停；nano 不引入
- **活跃 subagent 全局注册表 + TUI 观察层**：V23.4 简化版若做，仅维持一个进程内 dict（用于 `/agents` 命令）；不持久化

---

## 2. 各版本详细设计

详见后续小节（V23.0–V23.4 各自独立小节）。每档结构固定为：

1. **上一版本痛点**（具体到代码 / 体验细节）
2. **本档解决方法 + 核心抽象**
3. **新引入的概念**（与源项目对齐的术语）
4. **对应源项目**（文件路径 + 行号范围）
5. **本档暴露的新问题**（引出下一档；终点档则说明"为什么停在这里"）
6. **简化掉的**（vs 源项目）
7. **预估规模 + 验证项**

---

## V23.0 — 单任务 delegate（最小可用版）

### 上一版本痛点（V22 暴露）

V22 把 `chain.stream_call` 闭环了，但 agent 仍是**单线程顺序响应**：

- 用户问 "帮我同时看一下 [terminal_tool.py](../../tools/terminal_tool.py)、[skill_view_tool.py](../../tools/skill_view_tool.py)、[memory_store.py](../../tools/memory_store.py) 的设计"，agent 只能一个 turn 一个 turn 串行 read_file，三次 LLM 调用累计 30+ 秒
- 没有"父子隔离"原型：每加一个新工具，工具自身上下文（read 缓存 / terminal 会话）都污染同一份 `messages`
- V22 引入的 `CancelToken`（`threading.Event`）跨线程特性还没有真正的消费方 —— 注释里写"为 V23 准备"，但 V22 内部其实只用了单线程语义

### 解决方法 + 核心抽象

新增一个普通 LLM 工具 `delegate_task(goal, context, tools)`：

- 父 agent 在 tool calling 阶段决定要不要委派；这一档**只支持单任务**（没有 `tasks: []` 数组）
- 工具 handler 在父进程**同步**构建并跑一个新的子 `AIAgent`：
  - `messages = [{"role": "system", "content": child_system_prompt}, {"role": "user", "content": goal}]`
  - `child_system_prompt` = 固定骨架（"You are a sub-agent. Focus only on the goal below."）+ goal + context（不复用父三段式 PromptBuilder 的 skill 索引段，避免漏给越权指令）
  - 子 agent 复用父的 `TransportChain`、`ToolRegistry`，**不**复用父的 `MemoryManager` / `messages`
- 工具白名单 = 父全集减去硬编码黑名单（这一档黑名单写死，不暴露 schema 字段）：
  ```python
  DELEGATE_BLACKLIST = {
      "delegate_task",   # 防递归（V23.0 单层）
      "memory_*",        # 防子写脏父知识图谱
  }
  ```
- 子 agent 跑到自然终止（`finish_reason="stop"` 且无 pending tool_call）或达到 `max_iterations`（默认 8）后停下；**返回值 = final assistant text 拼成的纯字符串**，再用 `tool_result(output=...)` 包装

### 新引入的概念

- **子 AIAgent 实例**：第一次出现"父子两个 agent loop 同进程共存"的形态。V23.0 只跑同步串行（父调 delegate 工具 → 子 loop 跑完 → 返回 → 父继续），但 LL 的代码结构必须为后续档铺好路（child 构造和运行函数已经分离）
- **工具黑名单**：相对 V3 toolsets 的 "白名单挂载"，黑名单是"显式删去某些权限"。V23.1 会把它从硬编码升级为 schema 可选字段
- **聚焦的子 system prompt**：与父三段式 PromptBuilder（V21.2）相对的"瘦版本"——从父系统里复用 `system_skeleton`，但**不**注入 skill 索引（避免子越权调用 `skill_view`）和工具列表段（子的工具列表由它自己的 messages 注入）

### 对应源项目

- 工具 schema 极简版：`tools/delegate_tool.py:2626-2743`，只取 `goal` / `context` 字段
- 子构建主线程：`tools/delegate_tool.py:865-1158` 的 `_build_child_agent`，nano 版裁掉凭证覆盖、心跳、ACP 传输三块
- 子运行：`tools/delegate_tool.py:1305-1593` 的 `_run_single_child` 的"非线程池路径" —— 直接 `child.run_conversation(goal)` 同步跑
- 工具黑名单参考：`tools/delegate_tool.py:40-48`，nano 取其中 `delegate_task / memory_*` 两条；`clarify / send_message` 在 nano 不存在，跳过

### 暴露的新问题（引向 V23.1）

- 用户问 "同时分析 A、B、C 三个文件" → V23.0 仍要求父连续调三次 `delegate_task`，每次都同步阻塞，比 V22 串行 read_file 还慢（多了一次 child loop 启动开销）
- 工具黑名单写死在常量里，无法按场景调整（如 "只让子读文件，连 terminal 都禁掉"）
- 父全程 blocking 等子，UI 完全不响应

### 简化掉的（vs 源项目）

- 不做 `tasks: []` 数组（V23.1）
- 不做 `toolsets` schema 字段（V23.1）
- 不做 `role` 字段（V23.4 候选，默认 leaf 即可）
- 不做超时（这档让用户用 max_iterations 兜底；V23.2 引入 cancel）
- 不做心跳 / 诊断转储 / 文件读写跟踪 / token 统计 / 工具轨迹（V23.3）
- 不做凭证覆盖（永远用父 chain）

### 预估规模

`tools/delegate_tool.py` ~150 行 + child system prompt ~30 行 + 集成到 ToolRegistry / system_prompt 工具列表 ~20 行 + 决策日志。

### 验证项（`scripts/test_v23_0_delegate.py`）

1. **隔离**：父调一次 `delegate_task(goal="读 README 第一行")`，子返回后父 `messages` 中只看到一条 `tool_call` + 一条 `tool_result`，**不**包含子 agent 内部的 read_file 中间步
2. **黑名单生效**：手动构造 `delegate_task(goal="再 delegate 一次")`，子的 ToolRegistry 视图里 `delegate_task` 不可见，子 LLM 拿到的 tool list 不含该工具
3. **memory 黑名单**：父挂着 `MEMORY_SERVICE_URL` 启动，子 agent 跑完后 mock server `/retain` 调用次数为 0
4. **聚焦 system prompt**：子的 `messages[0]["content"]` 必须包含 `goal` 字面量；不得包含父三段式 prompt 的 "skill 索引" 段标识
5. **transport 复用**：用 fake transport 计数器验证父子共用同一个 `TransportChain` 实例（cache 统计累计、断路器状态共享）
6. **stream off 路径**：`STREAM_ENABLED=0` 时，父调 `delegate_task` 走 V21 同步 `chain.call`，全链路无 `stream_call` 出现
7. **stream on 兜底**：`STREAM_ENABLED=1` 时，父用 `chain.stream_call`，子内部仍走 `chain.call`（V23.0 子不流式），父 UI 表现为"等子 agent 时一片空白"——记录此现象，作为 V23.2 的痛点起点

---

## V23.1 — 批量并行 + 工具子集白名单

### 上一版本痛点（V23.0 暴露）

V23.0 完成了"父子隔离 + 单任务" 的最小骨架，但暴露两条具体问题：

- **批量场景下 V23.0 比 V21 还慢**：用户问 "并行分析 A/B/C 三个文件并对比"，父 LLM 必须连串发起 3 次 `delegate_task`，每次父都阻塞等子完成。3 次串行子调用累计的 LLM 启动开销比 V21 单 turn 串行 `read_file_tool` 还高 —— V23 的"并发"承诺没兑现
- **工具黑名单写死，无法收紧**：希望子 agent "只能读文件，禁掉 terminal" 时，V23.0 没法表达；只能改源码常量

### 解决方法 + 核心抽象

**A. 批量任务 schema + 单批分支**

`delegate_task` 工具新增可选字段 `tasks: [{goal, context, tools?}, ...]`：

- 不传 `tasks`、只传 `goal` → 单任务模式（V23.0 行为，向下兼容）
- 传 `tasks` → 批量模式：父进程主线程**先**串行构建所有子 `AIAgent`（共享父 transport / tool registry / cancel token；线程不安全的初始化全部在主线程做完），**再**用 `concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT)` 并行 `child.run_conversation(goal_i)`

`MAX_CONCURRENT` 默认 3（env `DELEGATE_MAX_CONCURRENT` 可调），来源：源项目 `delegation.max_concurrent_children` 默认值，这一档不暴露 schema 字段（避免父 LLM 滥用并发）。

返回值仍是字符串（保持 V23.0 协议），但单 vs 批分流：

- 单：返回 `tool_result(output=child_summary_text)`
- 批：返回 `tool_result(output=json.dumps([{"task_index": i, "summary": text_i}, ...]))`

V23.3 才把 `summary` 升级到结构化字段；V23.1 仅做"能并行 + 父能区分单 vs 批"。

**B. 工具子集 schema 字段 `tools`**

新增可选字段 `tools: ["read_file", "skill_view"]`（白名单），单任务在顶层、批任务在每个 `tasks[i]`：

- 不传 → 默认行为 = 父全集 - 黑名单（V23.0 行为）
- 传 → 取与父全集的**交集**（子永远拿不到父没装的工具，避免 LLM 编造工具名）；黑名单仍**强制减去**（即便父 LLM 在 `tools` 里写了 `delegate_task`，子也拿不到）

实现位置：构建子 `AIAgent` 时不直接传父 `ToolRegistry`，而是构造一个 `FilteredToolRegistry`（轻量包装：转发 `dispatch`，只重写 `list_schemas`）。

> **落地修订（V23.1 实施时）**：跳过 `FilteredToolRegistry` 类。V23.0 的 `run_child_loop(allowed_tool_names: set[str], ...)` 已经把"per-call 工具过滤"做成了一等参数（`get_definitions` 拉 schema 时 + dispatch 前白名单二次校验两处都过 set），相当于现成的 FilteredToolRegistry 成品。V23.1 直接在 `_resolve_child_toolset(requested=...)` 加一个可选参数实现"白名单 ∩ 父全集 - 黑名单"。详见 [docs/decisions/v23.1.md](../decisions/v23.1.md) 选型 1。

### 新引入的概念

- **批量 schema 的单 vs 批分支**：源项目同一函数内 if/else 分发，nano 跟进同款写法（教学上明确"两条路径，单是批的退化"）
- **`ThreadPoolExecutor` 在 agent loop 中的角色**：第一次出现"主线程构建 + worker 线程跑 LLM 调用"的形态。强调"线程安全责任分层"——构建期主线程独占，运行期 worker 不修改父 agent 状态
- **白名单交集语义**：用户传的 `tools` 是 "**最大允许集合**"，最终生效集 = `min(tools, parent_full) - blacklist`。这与 V3 toolsets 的"挂载式白名单"不同（V3 是 "至少包含"，V23.1 是 "至多包含"）
- **`FilteredToolRegistry` 包装**：避免改动父 registry；为 V23.4 嵌套（不同层子 agent 的允许集不同）打底

### 对应源项目

- 批量分发：`tools/delegate_tool.py:2071-2193`（主线程构建 → ThreadPoolExecutor 提交 → 轮询 futures → 收集排序）
- 单批分支：`tools/delegate_tool.py:2031-2069`（构建循环）+ `2071-2075`（单任务直接 `_run_single_child`）vs `2076-2193`（批量）
- 工具白名单交集：`tools/delegate_tool.py:940-963` 的 `_resolve_child_toolsets`
- 黑名单常量：`tools/delegate_tool.py:40-48` 的 `DELEGATE_BLOCKED_TOOLS`

### 暴露的新问题（引向 V23.2）

- 父跑批 3 个子，其中一个子 agent 进死循环（误用工具反复 retry）—— 父按 Ctrl+C，V22 翻译成 `cancel_token.cancel()`，但 V23.1 父子共用一个 token？还是子各自独立？没设计；当前是**父侧 cancel 不会传给子**，子继续跑到 `max_iterations`
- 父等批任务的 1–3 分钟里 UI 完全静默，看不到"哪个子已完成、哪个还在跑"
- 父调用 `delegate_task` 时 STREAM_ENABLED=1，子内部仍走同步 `chain.call`，浪费了 V22 的流式基建

### 简化掉的（vs 源项目）

- 不做心跳（防网关超时；nano 无 gateway）
- 不做 `max_concurrent_children` 暴露给 schema（仅 env 调）
- 不做 0-API-call 超时诊断转储（V23.3 部分覆盖）
- 不做返回结果按 `task_index` 排序（V23.1 用 `executor.map` 自然有序；V23.2 加进度时再换 `as_completed` 并排序）

### 预估规模

V23.0 基础上 + ~80 行（批量分发 + ThreadPoolExecutor）+ ~50 行（FilteredToolRegistry）+ ~30 行（schema 验证 + 黑白名单逻辑）。累计 V23.0+V23.1 ≈ 330 行。

### 验证项（`scripts/test_v23_1_batch.py`）

1. **批量并行确实更快**：3 个子任务，每个故意 sleep 2 秒（用 fake transport 模拟 LLM 延迟）。串行 V23.0 调用 3 次 ≈ 6 秒；V23.1 批量 ≈ 2 秒（误差 ±0.5s）
2. **结果顺序与 tasks 数组对齐**：批量返回的 JSON 数组 `[i].task_index == i`，即便 task_index=2 比 task_index=0 先完成
3. **白名单交集**：父全集 = `{read_file, terminal, skill_view, ...}`，传 `tools=["read_file", "nonexistent"]` → 子实际拿到 `{read_file}`（`nonexistent` 不在父全集，被丢弃，且不报错）
4. **黑名单强制**：传 `tools=["read_file", "delegate_task"]` → 子拿到 `{read_file}`，`delegate_task` 被强制剔除（即便用户白名单包含它）
5. **构建期错误不污染并发**：批量 5 个任务，第 3 个的 `tools` 里包含语法错误（非字符串数组）→ 整个 `delegate_task` 调用直接 `tool_error(...)`，不启动任何子 agent；前两个不会"半启动"
6. **回归 V23.0**：所有 V23.0 验证项重跑通过；不传 `tasks` 时行为完全一致

---

## V23.2 — 流式中继 + 中断传播

> **nano 落地编号**：本规划档的 V23.2，在 nano 主轴里实际落地为 **v23.3**
> （commit / banner / 测试脚本名 / 决策日志均用 `v23.3`）。原因：编号 v23.2
> 在并行迭代时被 [项目上下文注入 + --cwd](../decisions/v23.2.md) 先到先得占用。
> iteration-plan 内部行号保留不变以避免大面积回改；落地详情见
> [docs/decisions/v23.3.md](../decisions/v23.3.md)。

### 上一版本痛点（V23.1 暴露）

V23.1 让批量并行真正落地，但和 V22 流式之间**完全脱节**：

- 父调 `delegate_task` 时如果 `STREAM_ENABLED=1`，**父外层是流式**（用户看到父推理在打字），但**子内层走同步 `chain.call`**。结果是：父 LLM 一旦决定 delegate，UI 立刻冻结 1–3 分钟，体感上比 V22 之前更糟（用户已经习惯 V22 的实时反馈，此时反而觉得"agent 卡死了"）
- 子 agent 死循环没法 kill：父按 Ctrl+C 后，V22 把 SIGINT 翻译成父侧 `cancel_token.cancel()`，父侧 stream 抛 `StreamCancelled` 回到 prompt —— 但**正在 worker 线程跑的子 agent 完全不知道**，它会继续吃 token 直到 `max_iterations`
- 批量 3 个子 agent 同时跑，UI 上看不到"task#0 已完成 / task#1 read_file 中 / task#2 还在等 LLM 第一帧"，黑盒等待

### 解决方法 + 核心抽象

**A. 父子 `CancelToken` 桥接**

V22 的 `CancelToken` 已经是 `threading.Event`（专门为本档准备）。本档第一次真正消费这个特性：

- `delegate_task` 工具 handler 入参增加 `parent_cancel_token: CancelToken`（来自父 dispatch 上下文）
- 主线程构建子 agent 时，每个子拿到的是**同一个**父 token（不是各开一个） —— 父侧一次 cancel，所有子同步收到
- 子 agent loop 在 `chain.stream_call` / 工具调用边界都已经检查 `cancel_token.is_set()`（V22 已实现）；本档无新逻辑，只是把父 token "穿"下去
- 子流式中收到 cancel → 抛 `StreamCancelled`（V22 行为）→ delegate 工具 handler catch 后把该子的结果标记为 `status="interrupted"`，但**不**重抛（让父继续处理其他还在跑的子）
- 全部子结束后，如果父 token 仍处于 cancel 状态，delegate 工具 handler 才把汇总结果返回给父（让父走"用户主动 cancel"路径，回到 prompt）

**B. 子 stream event 中继到父 UI（progress callback）**

构建子时注入一个 `progress_callback: (child_index, event) -> None`：

- 子 `AIAgent.run_conversation` 内部用 `chain.stream_call` 时，每收到一个 `StreamEvent`（`text_delta` / `tool_call_started` / `done`）调一次 callback
- callback 主线程实现：把事件转成单行进度文本（如 `[task#1] read_file: tools/registry.py`）写到父 stderr（不污染 stdout 流式正文）
- 多线程并发写：用 `threading.Lock` 串行化输出，避免行交织
- `text_delta` 事件**不**逐字符回显（噪音太大），仅在 `tool_call_started` 和 `done` 两个里程碑事件回显
- 单任务模式下不显示 `[task#0]` 前缀，直接 `[delegate]`；批任务模式才带索引

**C. delegate 工具自身的流式行为**

`delegate_task` 工具 handler 是同步函数（V21.4 工具协议），但它内部跑批量 + 等 futures。本档**不**让 delegate 工具自身变成流式工具（保持 V21.4 工具协议不破坏）—— 流式中继只发生在 progress callback 写父 stderr 这一侧路径，**不**经过工具 handler 的返回值。

这与源项目一致（`tools/delegate_tool.py:678-862` 的 `_build_child_progress_callback` 是侧路输出到 CLI spinner，不通过 tool_result 流回父 LLM）。

### 新引入的概念

- **跨线程共享 cancel token**：把 V22 提前埋的 `threading.Event` 真正用起来。教学要点："为什么 V22 不用 `bool` 当 cancel 标志？因为多线程下 bool 写入的可见性无保证。"
- **侧路 progress 输出 vs 主路 tool_result**：第一次出现 "工具有两条输出通道"。主路（tool_result）是给父 LLM 看的、是协议；侧路（stderr progress）是给用户看的、是体验。两条通道独立，互不污染
- **fan-in lock**：多 worker 并发写一个共享输出流的最朴素同步原语；引出 V23.3 "为什么不用 queue + 单消费者"的问题作为后续优化空间
- **`StreamCancelled` 在子内层透传 → 工具 handler 翻译为状态字段**：V22 是"父侧抛到顶层回 prompt"，V23.2 是"子侧抛到 delegate handler 翻译成 `interrupted` 状态"。同一异常在不同语义层有不同处理

### 对应源项目

- progress callback 构建：`tools/delegate_tool.py:678-862` 的 `_build_child_progress_callback`，nano 版裁掉 ACP / gateway / TUI 三种 sink，只保留 stderr
- 中断传播：`tools/delegate_tool.py:2104-2139`（父 cancel 检测 + 子 token 设置）+ `1500-1507`（子内部 cancel 检查）
- StreamCancelled 翻译：`tools/delegate_tool.py:1802-1824` 的 `status="interrupted"` 分支
- token 共享设计的源代码注释：源项目用 `child._interrupt_requested = True` 是因为子 agent 不一定支持 streaming；nano 因为 V22 已经统一 `CancelToken`，可以直接共享 token

### 暴露的新问题（引向 V23.3）

- 父调 delegate 完，看到子返回 `"已完成 read_file"` 一句话，但**不知道子内部跑了几次 LLM 调用、用了多少 token、读了哪些文件**。复用 V22 cache 后这个信息更重要（要看子是否真的吃到了缓存）
- 多个子 agent 跑完，父侧 `session_estimated_cost_usd`（如果有）没有把子的 token 算进去，账目不全
- progress callback 的输出是临时的（写完即丢），如果用户 scroll 上去看历史 turn 的 delegate 调用，看不到当时的进度细节

### 简化掉的（vs 源项目）

- progress sink 只支持 stderr 一种，不支持 ACP / gateway / TUI 渲染层
- 不显示子的 reasoning_delta（V22 的 thinking 内容也只是输出一个 `[think] ...` 占位）
- 不做"子 agent 死锁检测"（源项目 `_run_single_child` 里有 30s 心跳；nano 同进程线程池不需要）
- 不做"延迟超过 N 秒的子任务自动 timeout 取消"（V23.3 才考虑）

### 预估规模

V23.1 基础上 + ~60 行（progress callback 构建 + 中继逻辑）+ ~20 行（cancel token 桥接，主要是参数穿透）+ ~30 行（fan-in lock + status 翻译）+ 测试。累计 V23.0+V23.1+V23.2 ≈ 440 行。

### 验证项（`scripts/test_v23_2_streaming.py`）

1. **父子共享 token**：构造一个故意 sleep 5 秒的 fake transport，父启动批量 3 个子任务，0.5 秒后主动 `parent_token.cancel()`。预期：3 个子全部在 1 秒内退出（不是 5 秒），返回结果中 `status="interrupted"`
2. **取消不影响兄弟子**：用真实 `chain.stream_call` 跑批量 3 个子，其中 task#1 故意构造一个超长 tool 调用（fake `time.sleep(10)` 工具）。模拟 task#0 完成、task#2 还在跑、用户 cancel → task#1 立即 interrupted；task#2 仍能正常完成或被 cancel（取决于 cancel 时机），关键是 task#0 的已完成结果不丢
3. **progress 中继不交织**：批量 5 个子并发，每个子至少触发 3 个 `tool_call_started`。`stderr` 抓取的 15+ 行进度全部以 `[task#i]` 开头且没有半行截断
4. **stream off 路径不挂 callback**：`STREAM_ENABLED=0` 时，子 agent 仍走 `chain.call`，progress callback **不**被调用一次（不是空操作 callback，是真的不挂）
5. **delegate 工具协议未破坏**：V21.4 的 dispatch 兜底（异常 / 非 str / 非 JSON）对 delegate 工具仍生效；progress 输出走 stderr，绝不污染 `tool_result(output=...)` 字符串
6. **回归 V22 + V23.0/V23.1**：105 项 V17–V21 不变量 + 13 项 V22 + V23.0/V23.1 全部测试零回归
7. **prompt 上 Ctrl+C 仍走 KeyboardInterrupt**：V22 的语义保留（父在 prompt 上按 Ctrl+C → 退出 agent；流式期间按 → cancel 当前响应回到 prompt）；V23.2 在"父等批量子任务时" Ctrl+C 走 cancel 路径，父 UI 走"用户主动取消"分支返回 prompt

---

## V23.3 — 结构化结果 + 成本聚合

### 上一版本痛点（V23.2 暴露）

V23.2 让父 UI 不再静默，子能被 kill，但**结果回传仍是黑盒一行字符串**：

- 父调子 agent 让它"分析三个 transport 文件"，子返回 `"已完成。chat_completions 走 SSE，anthropic 走 SDK，故障切换在 chain"`。父 LLM 后续要决定 "再让谁查源码 / 是否需要补充" 时，**完全不知道子这趟用了多少 token、读了哪些文件、是不是中途有工具失败**
- V20 引入 prompt cache 后，子 agent 是否真的吃到 cache、命中率多少，无法验证 —— V23.0–V23.2 完全没把子的 usage 累计回父
- 子 agent 跑超时（V23.2 仍无显式超时，仅靠 cancel）vs 跑到 max_iterations vs 自然完成，三种状态都被混淆为"返回了一个字符串就算完成"

### 解决方法 + 核心抽象

**A. 结构化结果字典**

`delegate_task` 返回值从纯字符串升级为结构化 JSON。单任务结果：

```json
{
  "status": "completed",
  "summary": "已完成。chat_completions 走 SSE，anthropic 走 SDK，故障切换在 chain。",
  "exit_reason": "completed",
  "duration_seconds": 12.4,
  "iterations": 3,
  "tokens": {"input": 1842, "output": 256, "cache_read": 1500, "cache_write": 0},
  "tool_trace": [
    {"tool": "read_file", "args_preview": "{\"path\":\"transports/chat_completions.py\"}", "result_bytes": 4218, "status": "ok"},
    {"tool": "read_file", "args_preview": "{\"path\":\"transports/anthropic.py\"}", "result_bytes": 5102, "status": "ok"},
    {"tool": "read_file", "args_preview": "{\"path\":\"transports/chain.py\"}", "result_bytes": 8321, "status": "ok"}
  ]
}
```

批量结果是上述 dict 的数组，每条带 `task_index`。整体仍用 `tool_result(output=json.dumps(...))` 包装，符合 V21.4 工具协议（**不**触发 dispatch 的"非 str / 非 JSON"兜底，因为 output 字段值本身是 JSON 字符串）。

`status` 五态枚举：`"completed" / "max_iterations" / "interrupted" / "error" / "timeout"`。`exit_reason` 与 `status` 大多重合，但保留两个字段是为了兼容源项目术语（`status` 是粗分类，`exit_reason` 是精细原因，便于未来扩展）。

**B. 成本聚合**

子 agent 跑完后，由 `delegate_task` handler 把子 `child.session_total_tokens` / `child.session_estimated_cost_usd`（V20 prompt cache 已经维护这两个字段）累加到 `parent.session_total_tokens` / `parent.session_estimated_cost_usd`。

聚合时机：每个子完成（无论成功 / 失败 / interrupt），单独 finalize 累加；不等批量整体结束（避免某个子 hang 导致已完成的 token 算不进父成本）。

**C. tool_trace 收集**

子 agent 内部维护一个轻量 trace 列表：`agent.run_conversation` 每次 dispatch tool 后追加一条 `{tool, args_preview, result_bytes, status}`。由 `delegate_task` handler 在收尾时取走（`child.pop_tool_trace()`）。

`args_preview`：`json.dumps(args)[:200]` 截断 + 末尾省略号；避免大参数撑爆 result。
`result_bytes`：`len(tool_result_str.encode("utf-8"))`，不含完整结果（结果可能很大）。
`status`：`"ok" / "error"`，由工具结果是否为 `tool_error()` 决定（V21.4 协议有 `error` 字段即视为 error）。

### 新引入的概念

- **结构化结果协议**：与 V21.4 的"工具结果都是合法 JSON"协议**复合** —— delegate 是"返回一个 output 字符串、字符串自身又是 JSON"的二级嵌套。父 LLM 看到的是字符串，但被设计为可 `json.loads` 的二次结构（教学要点：协议层级 vs 内容层级）
- **session-level 状态聚合**：父 agent 的 `session_*` 字段第一次开始接收"非父自身产生"的数据。这暗示 V24 trajectory 的设计——子 agent 也要有独立 trajectory 文件，父 trajectory 通过引用关联
- **`status` vs `exit_reason` 双字段**：为可观测性留扩展空间，避免"以后想加新状态时旧字段语义被冲淡"
- **轻量 trace vs 完整 trajectory**：trace 只存"调了什么 + 大小 + 是否成功"，不存完整内容；完整 trajectory（messages 全文）留给 V24 jsonl 落盘

### 对应源项目

- 结果 dict 结构：`tools/delegate_tool.py:1668-1800`，nano 版裁掉 `files_read` / `files_written` / `output_tail` 三块（前两个需要 file_state 模块，nano 没有；output_tail 是为 gateway 调试，nano 不需要）
- 成本聚合：`tools/delegate_tool.py:2231-2279` 的 `_aggregate_child_cost_into_parent`
- tool_trace：`tools/delegate_tool.py:1621-1653`，nano 简化为子 agent 自己维护数组，dispatch 后 append
- status 枚举：`tools/delegate_tool.py:1802-1824` 的状态分支

### 暴露的新问题（引向 V23.4 / V24）

- 子 agent 自己可以再调 `delegate_task` 吗？V23.0–V23.3 一直在黑名单里，无法多层规划（如父 → orchestrator 子 → leaf 孙）→ 引向 **V23.4 嵌套**
- 子的完整对话（不止 tool_trace 摘要）没有落盘 → 引向 **V24 trajectory**
- 跨会话看"过去 7 天 delegate 调用频次 / 平均子 token / 失败率" → 引向 **V24 insights**

### 简化掉的（vs 源项目）

- 不做 `files_read` / `files_written` 跟踪（依赖 file_state 模块）
- 不做 `output_tail`（最后 8 条工具结果预览）
- 不做 0-API-call 超时的诊断 dump 落盘（`~/.hermes/logs/subagent-timeout-*.log`）
- 不做完整 cost breakdown（按 transport / model 分摊）—— V24 insights 才做
- 不做 `start_time` / `end_time` 双字段（只保 `duration_seconds`）

### 预估规模

V23.2 基础上 + ~80 行（结果 dict 构造）+ ~40 行（成本聚合）+ ~50 行（tool_trace 收集 + child API）+ 测试。累计 V23.0+V23.1+V23.2+V23.3 ≈ 610 行（与 roadmap 预估"400-600 行"基本吻合，略超是因为 V22 流式集成多花了 ~60 行）。

### 验证项（`scripts/test_v23_3_structured_result.py`）

1. **结果可解析**：`tool_result["output"]` 是 JSON 字符串，`json.loads` 后符合 schema（`status` / `summary` / `tokens` / `tool_trace` 字段全部存在）
2. **token 累计**：父发起子任务前 `parent.session_total_tokens["input"] = 0`；子跑完读了 3 个文件，`parent.session_total_tokens["input"] >= sum(child_calls.input)`，差值 ≤ 父自身的 turn 消耗
3. **cache 字段穿透**：用 V20 cache 的 fake transport 模拟子第二次调用 `cache_read=1500`，父 `session_total_tokens["cache_read"]` 累加准确
4. **status 五态全覆盖**：分别构造 5 个测试用例（自然完成 / max_iterations / cancel / 工具抛异常 / fake timeout）→ 5 种 `status` 字面量全部出现
5. **批量聚合不漏**：批量 3 个子分别为 completed / interrupted / error → 父 token 累加 = 3 个子之和；前两个 summary 完整，error 那条 `summary` 字段含错误描述
6. **tool_trace 截断**：构造一个 args 长度 5KB 的工具调用 → tool_trace 中 `args_preview` 长度 ≤ 210（200 + 省略号 + JSON 引号开销）
7. **JSON 双层不破协议**：父 LLM 视角下 `tool_result["output"]` 仍是合法 JSON 字符串；V21.4 dispatch 兜底**不**触发（既不是异常，也不是非 str，也不是非 JSON）

---

## V23.4（可选） — 嵌套 delegate + 深度限制

> **优先级**：低于 V24 trajectory。V23.4 只在以下两种条件满足时启动：
>
> 1. V24 已完成（数据飞轮已建好），且
> 2. 出现真实使用场景（用户/教学需要 "先调研再实施" 这种 2 层任务分解）
>
> 否则跳过，留作 V25+ 的可选追加。

### 上一版本痛点（V23.3 暴露）

V23.3 仍是**严格扁平结构**：父能 spawn 多个并行子，但每个子无法再分解任务。

复杂任务比如"梳理 nano 项目所有工具的 schema 一致性"需要分两层：

- 一个 orchestrator 子负责规划（"先列工具清单，再对每个工具检查 schema"）
- 多个 leaf 子并行执行 schema 检查

V23.3 下，父 LLM 必须自己做规划层，把规划逻辑塞在父的 messages 里 —— 父 context 被规划细节污染，违反 V23 "父子隔离"的初衷。

### 解决方法 + 核心抽象

**A. `role` 字段**

`delegate_task` 新增可选字段 `role: "leaf" | "orchestrator"`，默认 `"leaf"`：

- `leaf`：子 ToolRegistry 不含 `delegate_task`（V23.0–V23.3 行为）
- `orchestrator`：子 ToolRegistry **含** `delegate_task`，子可再 spawn 自己的 leaf

**B. `max_spawn_depth` 限制**

env `DELEGATE_MAX_DEPTH` 默认 2（父=0，子=1，孙=2）。每个子 agent 携带一个 `_spawn_depth` 属性：

- 父 agent 自身 `_spawn_depth = 0`
- 父调 `delegate_task` 构建子时，子 `_spawn_depth = parent._spawn_depth + 1`
- 子内 `delegate_task` handler 进入时检查：`depth + 1 >= max_depth` → 强制把请求中的 `role="orchestrator"` 降级为 `"leaf"`，并在结果中加 warning：`{"warnings": ["spawn depth limit reached, role downgraded to leaf"]}`
- `depth >= max_depth` → 直接 `tool_error("delegate not allowed at this depth")`

### 新引入的概念

- **递归 + 深度计数**：第一次出现 agent 自身递归。需要小心**不让父的 `messages` 被孙 agent 污染**（V23.0 的隔离原则递归适用）
- **强制降级 vs 直接拒绝**：源项目用 "强制降级 + warning" 的设计是为了让父 LLM 不容易"卡住"——LLM 误用 role 时仍能跑，只是失去递归能力。nano 跟进同款选择
- **`max_depth=2` 的默认值依据**：3 层（父→子→孙）已经覆盖几乎所有教学场景；4+ 层在生产里都罕见

### 对应源项目

- 角色解析：`tools/delegate_tool.py:899-908`
- 深度检查：`tools/delegate_tool.py:905-908` + `389-424`
- 降级 warning：`tools/delegate_tool.py:402-422`

### 暴露的新问题（终点档）

V23 系列收尾。后续问题归属于其他子系统：

- "我想看历史 delegate 调用的完整 trajectory" → V24
- "我想让多个子 agent 用不同模型对同一问题投票" → V25 Mixture-of-Agents
- "我想给某些工具调用加审批" → V26 Approval / Hook

### 简化掉的（vs 源项目）

- 不做 `delegation.max_spawn_depth` 配置文件入口（仅 env）
- 不做暂停 `_spawn_paused`（源项目允许临时暂停所有 spawn，nano 不引入）
- 不做按 role 区分的不同 system prompt 模板（仍用 V23.0 同一模板）
- 不做"orchestrator 失败时自动重派给另一个 orchestrator"（源项目无此特性，nano 也不引入）

### 预估规模

V23.3 基础上 + ~40 行（role 字段 + 深度计数 + 降级逻辑）+ 测试。累计 V23.0–V23.4 ≈ 650 行。

### 验证项（`scripts/test_v23_4_nesting.py`）

1. **leaf 子拿不到 delegate_task**：`role="leaf"` 子的 ToolRegistry list 不含 `delegate_task`
2. **orchestrator 子能再 spawn**：`role="orchestrator"` 子调 `delegate_task` 成功创建孙 agent，孙 agent 的 messages 不含父或子的对话
3. **深度溢出降级**：`DELEGATE_MAX_DEPTH=2` 下，孙 agent（depth=2）请求 `role="orchestrator"` → 实际变 `"leaf"` + 结果含 warning
4. **深度溢出拒绝**：`DELEGATE_MAX_DEPTH=2` 下，孙 agent 直接调 `delegate_task` 创建曾孙 → `tool_error`
5. **回归 V23.0–V23.3**：默认 `role="leaf"` 不传时，所有前档验证项通过

---

## 3. 与已有版本的协同

| 版本 | 协同点 |
|------|--------|
| V1 ToolRegistry | `delegate_task` 注册为普通 LLM 工具；`FilteredToolRegistry`（V23.1）包装父 registry |
| V3 toolsets | V23.1 工具白名单交集语义与 toolsets 挂载语义并存（不冲突，各管一层）|
| V8 MemoryManager | 子 agent 默认黑名单 `memory_*`，避免子写脏父知识图谱（V23.0 起）|
| V14 会话切换 | 父 `/new` 时强制 cancel 所有进行中的子（V23.2 cancel 桥接的扩展）|
| V19 TransportChain | 父子共享同一个 chain 实例：cache 累计、断路器状态全局共享（V23.0 起）|
| V20 Prompt Cache | 子 agent 透明继承 `apply_prompt_cache` 行为；V23.3 把 cache_read/write 累计回父成本 |
| V21.2 PromptBuilder | 子 system prompt 用骨架段，**不**注入 skill 索引段（避免子滥用 skill_view）|
| V21.3 Skill | 子默认拿不到 `skill_view` 工具（不在父全集 ∩ 子白名单时被裁掉，且 skill 索引段不注入）— V25+ 若需要再放开 |
| V21.4 工具结果协议 | `delegate_task` 自身遵守 `tool_result(output=json.dumps(...))` 协议；V23.3 二级 JSON 不破坏协议层 |
| V22 流式 + CancelToken | 本系列最强前置：V23.2 父子共享 `CancelToken`，子内部直接复用 V22 的 `chain.stream_call` 流式路径 |

---

## 4. 主动延期 / 不做的方向

- **Mixture-of-Agents**（源项目 `tools/mixture_of_agents_tool.py`）：roadmap 已排到 V25。V23 系列做完后，MoA 的实现可以直接复用 V23.1 的 `ThreadPoolExecutor` 和 V23.2 的 `progress callback` 中继 —— 这是 V23 顺序在 MoA 之前的额外好处。
- **暂停 / 恢复 / 序列化**（源项目 `_spawn_paused` 等）：依赖持久化层；nano 教学版不必要。
- **ACP 跨进程子 agent**：源项目走 ACP 协议把子 agent 跑在独立进程；nano 同进程线程池足够，不引入跨进程通信。
- **TUI 观察层 / `/agents` 全局活跃表**：源项目维护进程级活跃 subagent 注册表 + TUI 渲染。如果 V23.4 落地，可顺手做一个内存级 dict + `/agents` 命令（仅显示当前正在跑的子任务索引、goal 摘要、已用秒数），不持久化。

---

## 5. 参考资料

- 本档对应 nano roadmap 节：[`docs/system-roadmap.md` § "V23 多智能体（delegate_task）"](../system-roadmap.md)
- 源项目入口：[hermes-agent/tools/delegate_tool.py](https://github.com/qshf/hermes-agent/blob/main/tools/delegate_tool.py)
- 前置档：[`docs/decisions/v22.md`](../decisions/v22.md)（V22 流式 + CancelToken 的设计基础）
- 后续档：V24 trajectory（数据落盘起点）、V25 MoA、V26 Approval、V27 Todo/Clarify




