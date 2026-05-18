# 从零搭建 AI Agent 工具系统 — 教学大纲

## 课程定位

面向有 Python 基础的开发者，通过 6 个迭代版本（V0→V5），从最简单的 if/elif 分发开始，逐步构建一个完整的 AI Agent 工具系统。每个版本解决上一版本暴露的具体问题，让学员理解"为什么要这样设计"而非"记住这个模式"。

**前置要求：** Python 基础、了解 OpenAI API 调用方式（function calling）、基本的 asyncio 概念。

**最终产出：** 学员能独立阅读生产级 Agent 框架的工具系统源码，理解其设计决策。

---

## 第一讲：最小可用 Agent（V0）

### 教学目标
- 理解 AI Agent 的核心循环：用户输入 → LLM 推理 → 工具调用 → 结果回传 → 继续推理
- 能跑通一个最简单的 Agent

### 内容

1. **什么是 Agent？** 与普通 chatbot 的区别：能"动手"（调用工具）
2. **核心循环**
   ```
   while True:
       response = llm.chat(messages, tools=...)
       if no tool_calls: break
       for call in tool_calls:
           result = execute(call)
           messages.append(result)
   ```
3. **V0 实现：if/elif 分发**
   - 2 个工具：terminal（执行命令）、read_file（读文件）
   - 工具定义硬编码在 agent.py 里
   - dispatch 用 if/elif 判断工具名

### 引出问题
- 每加一个工具要改 3 处代码（schema 列表、if 分支、import）
- 工具和 agent 强耦合，无法复用

### 动手环节
- 跑通 V0，让 Agent 执行 `ls` 命令
- 尝试加一个新工具（比如 write_file），体会改 3 处的痛苦

---

## 第二讲：注册表模式（V1）

### 教学目标
- 理解"自注册"模式：工具 import 时自动注册，agent 无需知道具体工具
- 理解 dispatch 的价值：统一入口，解耦调用方和实现方

### 内容

1. **问题回顾：** V0 加工具要改 3 处，工具和 agent 耦合
2. **解法：ToolRegistry**
   - `register(schema, handler)` — 存储工具定义和处理函数
   - `dispatch(name, args)` — 按名字查找并调用
   - `get_openai_tools()` — 返回所有工具的 schema
3. **自注册模式**
   - 每个工具文件底部调用 `registry.register(...)`
   - `tools/__init__.py` 自动扫描 `*_tool.py` 并 import
   - agent.py 只需 `from tools.registry import registry`
4. **加新工具的体验变化**
   - V0：改 3 处
   - V1：只写一个新文件，放到 tools/ 目录，完事

### 引出问题
- 所有工具都暴露给 LLM，上下文浪费（Docker 工具在没有 Docker 的机器上也出现）
- 无法按场景选择工具子集

### 动手环节
- 写一个新工具（如 `get_time_tool.py`），体验"放进去就能用"
- 对比 V0 和 V1 的 agent.py 差异

---

## 第三讲：工具组与运行时可用性（V2）

### 教学目标
- 理解 Toolsets 的作用：按场景分组，避免上下文膨胀
- 理解 check_fn 的作用：运行时探测工具是否可用

### 内容

1. **问题回顾：** 所有工具都暴露，Docker 工具在没 Docker 的机器上也出现
2. **Toolsets：名字展开**
   ```python
   TOOLSETS = {
       "core": ["terminal", "read_file", "write_file"],
       "docker": ["terminal", "read_file", "docker_exec"],
   }
   ```
   - agent 只声明 `ENABLED_TOOLSETS = ["core"]`
   - `resolve_toolsets()` 展开为具体工具名列表
3. **check_fn：运行时可用性**
   ```python
   def check_docker():
       return shutil.which("docker") is not None

   registry.register(schema, handler, check_fn=check_docker)
   ```
   - `get_definitions()` 过滤掉 check_fn 返回 False 的工具
4. **效果：** 同一份代码在不同环境自动适配

### 引出问题
- 每轮对话都要遍历注册表 + 调 check_fn
- check_fn 可能 fork 进程（如 `shutil.which`），每轮都调很浪费
- 无法感知注册表是否变化（每次都重算）

### 动手环节
- 给 docker_exec 加 check_fn，在没有 Docker 的机器上验证它不出现
- 加日志观察 check_fn 被调用的频率

---

## 第四讲：两层缓存 + MCP 动态加载（V3）

### 教学目标
- 理解 generation 计数器的缓存失效策略
- 理解 TTL 缓存的适用场景
- 理解 MCP 协议的基本概念和 stdio 传输

### 内容

1. **问题回顾：** 每轮都重算工具列表，check_fn 调用过于频繁
2. **外层缓存：generation key**
   ```python
   cache_key = (enabled_toolsets, registry.generation)
   # generation 没变 → 注册表没变 → 直接返回缓存
   ```
   - register/deregister 递增 generation
   - 命中时 ~0μs 返回
3. **内层缓存：check_fn TTL**
   ```python
   # 30s 内不重复调用 check_fn
   cached = self._check_fn_cache.get(name)
   if cached and (now - cached[0]) < 30:
       return cached[1]
   ```
4. **MCP 协议简介**
   - 什么是 MCP：标准化的工具服务协议
   - stdio 传输：agent 启动子进程，通过 stdin/stdout 通信
   - 核心流程：connect → initialize → list_tools → call_tool
5. **MCP 客户端实现**
   - 后台 daemon 线程跑 asyncio event loop
   - `run_coroutine_threadsafe` 桥接 sync agent ↔ async MCP
   - 连接后自动 register → generation 递增 → 缓存失效 → 下轮可见
6. **按需加载的价值**
   - 不是所有工具都需要，按需连接避免上下文过长
   - `/mcp connect` 命令演示

### 引出问题
- LLM 返回的参数类型经常不对（`"42"` 而非 `42`）
- 想写 async handler 但 agent 循环是 sync 的
- 每个 handler 各自做类型转换，重复且容易遗漏

### 动手环节
- 写一个 MCP server（FastMCP），提供 get_weather 工具
- 用 `/mcp connect` 连接，观察 generation 变化和缓存失效
- 用 `/mcp disconnect` 断开，观察工具消失

---

## 第五讲：类型修复 + 异步桥接（V4）

### 教学目标
- 理解为什么需要在 dispatch 层统一做类型转换
- 理解 sync/async 桥接的核心问题和解法
- 理解 `is_async` 标记的设计意图

### 内容

1. **问题回顾：** LLM 返回 `"42"`，handler 期望 `int`
2. **类型强制转换（coerce）**
   - 读取工具的 JSON Schema，知道每个参数的期望类型
   - 只转换 string → 其他类型（非 string 跳过）
   - 安全失败：转换失败保留原值
   ```python
   # dispatch 内部，handler 执行前
   coerced = coerce_args(entry["schema"], args)
   ```
3. **为什么放在 dispatch 层？**
   - 集中处理 vs 每个 handler 各自处理
   - 对比：如果 10 个工具都要 `int(args["timeout"])`，不如统一做一次
4. **异步桥接**
   - 问题：agent 主循环是 `while True` 同步循环，无法 `await`
   - 解法：`_run_async(coro)` 检测当前是否有 running loop
     - 无 → `asyncio.run(coro)`
     - 有 → 开新线程跑 `asyncio.run(coro)`
   - 注册时声明 `is_async=True`，dispatch 自动桥接
5. **对比 MCP 的异步方案**
   - MCP：长连接，需要持久 event loop → 自建 `_run_on_mcp_loop`
   - V4 async 工具：一次性协程 → `_run_async` 用完即走
   - 两种场景，两种解法

### 引出问题
- 工具调用没有统一的日志/限流/截断机制
- 想加功能只能改 dispatch 代码，不够灵活
- 插件 load 了不能 unload

### 动手环节
- 故意传错误类型参数，观察 coerce 自动修正
- 写一个 async 工具（如调用 httpx），验证 `is_async=True` 正常工作
- 对比有无 coerce 时 handler 代码的简洁程度

---

## 第六讲：插件钩子系统（V5）

### 教学目标
- 理解钩子模式：不改核心代码，通过注册回调扩展行为
- 理解三种钩子语义的区别（阻止 / 观察 / 替换）
- 理解插件生命周期（load/unload）的价值

### 内容

1. **问题回顾：** 想加日志？改 dispatch。想加限流？改 dispatch。想截断？改 dispatch。
2. **钩子模式的核心思想**
   - 不改核心代码，通过"挂钩子"扩展行为
   - 类比：Git hooks、React lifecycle、Express middleware
3. **三种钩子语义**
   | 钩子 | 时机 | 能力 |
   |------|------|------|
   | `pre_tool_call` | 执行前 | 可阻止（返回 block 指令） |
   | `post_tool_call` | 执行后 | 只观察（返回值忽略） |
   | `transform_tool_result` | 返回前 | 可替换（第一个非 None 字符串替换结果） |
4. **HookManager 实现**
   - `register(hook_name, callback)` / `deregister(hook_name, callback)`
   - `invoke(hook_name, **kwargs)` — 遍历回调，try/except 隔离
5. **dispatch 集成**
   ```
   coerce → pre_hook → handler → post_hook → transform → return
   ```
6. **插件生命周期**
   - 约定：插件导出 `register(hook_manager)` 和 `deregister(hook_manager)`
   - `load_plugin()` 加载模块 + 调用 register
   - `unload_plugin()` 调用 deregister + 清理 sys.modules
7. **三个示例插件**
   - logging_hook：打印调用日志（pre + post）
   - truncate_hook：截断长结果（transform）
   - rate_limit_hook：限流阻止（pre block）

### 动手环节
- 加载 logging_hook，观察每次工具调用的日志输出
- 加载 rate_limit_hook，快速连续调用触发限流
- 卸载插件，验证行为恢复
- 自己写一个插件（如：禁止执行 `rm` 命令的安全钩子）

---

## 第七讲：回顾与源项目对齐（总结）

### 教学目标
- 串联 V0→V5 的演进逻辑，理解每一步"为什么"
- 对比生产级实现，知道差距在哪、如何补齐

### 内容

1. **演进路线回顾**
   ```
   V0: if/elif（能跑）
    ↓ 问题：加工具改 3 处
   V1: 注册表（解耦）
    ↓ 问题：所有工具都暴露
   V2: Toolsets + check_fn（按需）
    ↓ 问题：每轮重算，check_fn 太频繁
   V3: 缓存 + MCP（性能 + 动态）
    ↓ 问题：类型错误，async 不支持
   V4: coerce + async bridge（健壮）
    ↓ 问题：无扩展点，无生命周期
   V5: hooks + plugin lifecycle（可扩展）
   ```

2. **核心设计模式总结**
   - 自注册 + dispatch（V1）
   - 名字展开 + 运行时探测（V2）
   - Generation 缓存失效 + TTL（V3）
   - Schema 驱动的类型修正（V4）
   - 钩子语义分层 + 错误隔离（V5）

3. **与源项目的差距**
   - 省略了什么：Gateway、Session、Skills、Config、安全、可观测性
   - 简化了什么：线程安全、重连、熔断、多策略 async
   - 为什么省略：教学聚焦工具系统核心，生产关注点按需添加

4. **如果要继续演进**
   - V6 方向：并行工具执行（ThreadPoolExecutor）
   - V7 方向：会话持久化（SQLite）
   - V8 方向：多 provider 适配（Anthropic / DeepSeek）

### 动手环节
- 阅读源项目 `tools/registry.py`，对比 V5 的实现
- 阅读源项目 `model_tools.py` 的 `_run_async()`，理解三策略设计
- 尝试给 V5 加一个新功能（如并行工具执行）

---

## 附录：教学节奏建议

| 讲次 | 时长 | 重点 | 代码量 |
|------|------|------|--------|
| 第一讲 | 45min | 跑通 Agent，理解核心循环 | ~100 行 |
| 第二讲 | 45min | 注册表模式，自注册 | +80 行 |
| 第三讲 | 30min | Toolsets，check_fn | +50 行 |
| 第四讲 | 60min | 缓存原理，MCP 协议 | +300 行 |
| 第五讲 | 45min | coerce，async bridge | +180 行 |
| 第六讲 | 45min | hooks，插件生命周期 | +200 行 |
| 第七讲 | 30min | 回顾，源项目对齐 | 0（阅读） |

**总计：** ~5 小时，适合半天 workshop 或分 7 次 session。

---

## 附录：每讲的"灵魂问题"

用这些问题检验学员是否真正理解：

1. **V0→V1：** "如果没有注册表，加第 20 个工具时 agent.py 会变成什么样？"
2. **V1→V2：** "为什么不在 register 时就决定工具是否可用，而要用 check_fn 运行时检查？"
3. **V2→V3：** "generation 计数器为什么比'每次都重算'好？它的代价是什么？"
4. **V3→V4：** "为什么 coerce 放在 dispatch 层而不是每个 handler 里？有没有不该 coerce 的情况？"
5. **V4→V5：** "如果没有钩子系统，想给所有工具加限流要改几处代码？有了钩子呢？"
6. **整体：** "MCP 工具用 `_run_on_mcp_loop`，V4 async 工具用 `_run_async`，为什么不统一？"
