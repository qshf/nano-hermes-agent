# nano_hermes_agent V5 与源项目（hermes-agent）全面差异对比

## 概述

nano_hermes_agent 是源项目 hermes-agent 的教学简化版，通过 V0→V5 六个迭代版本，
逐步还原源项目工具系统的核心设计模式。本文档对比最终版本 V5 与源项目的全部差异。

**规模对比：**

| | 源项目 | V5 (nano) |
|---|--------|-----------|
| 总代码量 | ~500K 行，1589 个文件 | ~1300 行，15 个文件 |
| 核心工具系统 | ~30K 行 | ~800 行 |
| 工具数量 | 100+ 个工具文件 | 4 个工具 + 3 个插件 |

---

## 一、工具注册表（V1 引入）

| 维度 | 源项目 `tools/registry.py` (563 行) | V5 `tools/registry.py` (169 行) |
|------|------|------|
| 数据结构 | `ToolEntry` 类（10 个字段） | 普通 dict（4 个字段） |
| 注册接口 | `register(name, toolset, schema, handler, check_fn, requires_env, is_async, emoji, max_result_size_chars, dynamic_schema_overrides)` | `register(schema, handler, check_fn, is_async)` |
| 线程安全 | RLock + snapshot 模式（并发读写安全） | 无锁（单线程 CLI） |
| 动态 schema | `dynamic_schema_overrides` 回调，运行时修改 schema | 无 |
| 工具元数据 | emoji、description、max_result_size_chars | 无 |
| Toolset 别名 | MCP server 名 → toolset 映射 | 无 |

**保留的核心模式：** 自注册（import 时 register）、dispatch 分发、generation 计数器。

---

## 二、工具组 Toolsets（V2 引入）

| 维度 | 源项目 `toolsets.py` (856 行) | V5 `toolsets.py` (~30 行) |
|------|------|------|
| 预定义组数 | 50+ 个（web, terminal, vision, browser, delegation, kanban 等） | 2 个（core, docker） |
| 组合机制 | `includes` 字段支持组合（debugging = web + file） | 无组合 |
| 平台组 | 20+ 平台专用组（telegram, discord, slack, whatsapp 等） | 无 |
| 特殊别名 | `"all"` / `"*"` 解析为全部工具 | 无 |
| 递归解析 | `resolve_toolset(name, visited)` 处理菱形依赖和循环 | 简单列表查找 |
| 插件自动生成 | 平台插件自动创建对应 toolset | 无 |
| check_fn | 每个 toolset 可有独立的可用性检查 | 每个工具独立 check_fn |

**保留的核心模式：** 名字展开（toolset name → tool names）、check_fn 运行时可用性检查。

---

## 三、缓存系统（V3 引入）

| 维度 | 源项目 `model_tools.py` (868 行) | V5 `model_tools.py` (~50 行) |
|------|------|------|
| 外层缓存 key | `(enabled_toolsets, disabled_toolsets, generation, config_mtime_fingerprint)` | `(enabled_toolsets, generation)` |
| Config 指纹 | `(st_mtime_ns, st_size)` 监测 config.yaml 变化 | 无 config 文件 |
| 缓存防污染 | 返回 `list(cached)` 浅拷贝 | 直接返回引用 |
| 内层 TTL | 30s，异常视为 False | 30s，异常视为 False |
| per-call 缓存 | 同一次 get_definitions 内去重 check_fn 调用 | 无 |

**保留的核心模式：** 两层缓存（外层 generation key + 内层 check_fn TTL）、generation 递增触发失效。

---

## 四、MCP 客户端（V3 引入）

| 维度 | 源项目 `tools/mcp_tool.py` (3408 行) | V5 `tools/mcp_client.py` (240 行) |
|------|------|------|
| 传输方式 | stdio + HTTP/StreamableHTTP + SSE | 仅 stdio |
| 后台 loop | daemon 线程 + 持久 event loop | 相同 |
| 重连机制 | 指数退避，最多 5 次 | 无重连 |
| 熔断器 | 连续失败后自动熔断 | 无 |
| 环境过滤 | 子进程只继承白名单环境变量 | 继承全部环境 |
| 凭证脱敏 | 错误信息中 redact secrets | 无 |
| Sampling | MCP server 可请求 LLM 补全 | 无 |
| RPC 锁 | per-server `_rpc_lock` 防并发调用 | 无 |
| Interrupt | 轮询式等待，支持用户中断 | `future.result(timeout)` 阻塞等待 |
| stderr 重定向 | 共享日志文件，防 TUI 污染 | 无 |
| 超时配置 | per-server tool_timeout + connect_timeout | 全局 360s |
| 工具命名 | `mcp_{server}_{tool}` | 相同 |

**保留的核心模式：** 后台 daemon 线程 + `run_coroutine_threadsafe` 桥接、connect → list_tools → register → call_tool 流程、disconnect 信号机制。

---

## 五、类型强制转换（V4 引入）

| 维度 | 源项目 `model_tools.py` (195 行) | V5 `tools/coerce.py` (135 行) |
|------|------|------|
| 基本类型 | int, number, bool, array, object | 相同 |
| Union type | `"type": ["integer", "string"]` 按序尝试 | 相同 |
| Array wrapping | 裸标量自动包装为单元素数组 | 无 |
| Null 处理 | `"null"` → `None`（schema 允许时） | 简单支持 |
| 嵌套对象 | 递归 coerce nested properties | 无递归 |
| 日志 | DEBUG 级别记录每次转换 | 无日志 |
| 失败策略 | 保留原值，不抛异常 | 相同 |

**保留的核心模式：** 根据 JSON Schema 自动修正类型、安全失败（fail-open）。

---

## 六、异步桥接（V4 引入）

| 维度 | 源项目 `model_tools.py` (135 行) | V5 `tools/registry.py` (~15 行) |
|------|------|------|
| 策略数 | 3 种 | 2 种 |
| 主线程 | 持久 `_tool_loop`（防 GC 关闭 loop） | `asyncio.run()`（每次新建 loop） |
| Worker 线程 | per-thread 持久 loop（thread-local） | 无 |
| Async 上下文 | 开新线程 + 独立 loop + 超时取消 | 开新线程 + `asyncio.run()` |
| 超时 | 300s，超时后 cancel tasks | 60s，无 cancel |
| Loop 生命周期 | 持久（防 httpx/AsyncOpenAI 绑定 loop 被关闭） | 临时（用完即弃） |

**保留的核心模式：** `is_async` 标记 + dispatch 自动桥接、检测 running loop 决定策略。

---

## 七、插件钩子（V5 引入）

| 维度 | 源项目 `hermes_cli/plugins.py` (1475 行) | V5 `tools/hooks.py` (68 行) |
|------|------|------|
| 钩子种类 | 15+ 种 | 3 种 |
| 插件来源 | 4 个（bundled, user, project, pip） | 1 个（plugins/ 目录） |
| 插件清单 | `plugin.yaml` + `__init__.py` | 约定 `register()` / `deregister()` |
| 插件种类 | 5 种 kind | 无分类 |
| PluginContext | 门面模式（register_hook, register_tool, get_config 等） | 直接传 hook_manager |
| 错误隔离 | try/except per callback | 相同 |
| Block 语义 | `{"action": "block", "message": "..."}` | 相同 |
| Transform 语义 | 第一个非 None 字符串替换 | 相同 |
| 依赖解析 | plugin.yaml 声明 dependencies | 无 |
| 卸载 | 支持 | 支持 |

**保留的核心模式：** 钩子语义分层（pre 可阻止 / post 观察 / transform 替换）、错误隔离、显式生命周期。

---

## 八、源项目有但教程完全省略的子系统

| 子系统 | 源项目规模 | 功能 |
|--------|-----------|------|
| Gateway 网关 | 185K 行，61 文件 | Telegram/Discord/Slack/WhatsApp/Signal 等 20+ 平台消息路由 |
| Session 持久化 | 2966 行 | SQLite + WAL + FTS5 全文搜索 + 会话压缩分裂 |
| Skills 系统 | 3772 行 | Markdown 技能文件 + frontmatter + 条件过滤 + 索引 |
| 配置系统 | 5186 行 | config.yaml 分层配置 + 动态重载 + schema 校验 + 迁移 |
| System Prompt | 1456 行 | 注入检测 + 威胁模式匹配 + 不可见 Unicode 检测 + 技能索引 |
| 终端后端 | 3759 行 | Docker / Modal / SSH / Vercel / Singularity / Daytona |
| 日志系统 | 389 行 | 结构化日志 + 轮转 + verbose 模式 + 关机取证 |
| Provider 适配 | ~2000 行 | OpenAI / Anthropic / DeepSeek / Copilot 多 provider 切换 |
| 子 Agent | ~1500 行 | delegate_task 工具 + 子 agent 生命周期 |
| Memory 系统 | ~3000 行 | 长期记忆存储 + 检索 + 上下文注入 |
| Kanban | ~1200 行 | 任务看板 + 状态流转 + 分配 |

---

## 九、dispatch 流程完整对比

### 源项目（分布在 model_tools.py + registry.py）

```
handle_function_call(tool_name, args):
    1. coerce_tool_args(tool_name, args)           # model_tools.py
    2. invoke_hook("pre_tool_call", ...)           # plugins.py → 可 block
    3. registry.dispatch(tool_name, args)          # registry.py
       └─ if is_async: _run_async(handler(args))  # model_tools.py
       └─ else: handler(args)
    4. invoke_hook("post_tool_call", ...)          # 含 duration_ms
    5. invoke_hook("transform_tool_result", ...)   # 第一个非 None 替换
    6. truncate(result, max_result_size_chars)     # ToolEntry 字段
    7. 返回
```

### V5（全部集中在 registry.dispatch）

```
dispatch(name, args):
    1. coerce_args(schema, args)                   # tools/coerce.py
    2. hook_manager.invoke("pre_tool_call", ...)   # tools/hooks.py → 可 block
    3. handler(coerced) 或 _run_async(...)         # 含计时
    4. hook_manager.invoke("post_tool_call", ...)  # 含 duration_ms
    5. hook_manager.invoke("transform_tool_result", ...)  # 第一个非 None 替换
    6. 返回
```

**关键差异：** 源项目将 coerce 和 hooks 放在 `model_tools.py`（调用层），dispatch 放在 `registry.py`（执行层），职责分离更清晰。V5 把所有逻辑集中在 `dispatch()` 一个方法里，牺牲分层换取可读性。

---

## 十、设计哲学差异

| | 源项目 | V5 (nano) |
|---|--------|-----------|
| 定位 | 生产级 AI Agent 框架 | 教学演示项目 |
| 用户 | 终端用户 + 20+ 消息平台 | 开发者学习 |
| 并发模型 | 多线程（gateway 并行处理多用户） | 单线程（交互式 CLI） |
| 错误处理 | 完善（超时、重试、熔断、降级、异常链展平） | 最小（try/except） |
| 配置 | YAML + 环境变量 + CLI 参数 + 动态重载 | 无配置文件 |
| 持久化 | SQLite WAL + FTS5 + 会话压缩 | 无（内存中） |
| 安全 | 注入检测、凭证脱敏、环境过滤、路径校验 | 无 |
| 可观测性 | 结构化日志 + 轮转 + verbose + 关机取证 | print 输出 |
| 测试 | 完整单元测试 + 集成测试 | 手动验证 |
| 跨平台 | Windows/macOS/Linux（UTF-8 bootstrap、pathlib、psutil） | macOS/Linux |

---

## 十一、各版本对齐的源项目文件

| 教程版本 | 核心概念 | 对应源项目文件 |
|---------|---------|---------------|
| V0 | if/elif 分发 | 无（源项目从未用过这种模式） |
| V1 | 自注册 + dispatch | `tools/registry.py` register/dispatch |
| V2 | Toolsets + check_fn | `toolsets.py` + `registry.py` _is_available |
| V3 | generation + TTL + MCP | `model_tools.py` 缓存 + `tools/mcp_tool.py` |
| V4 | coerce + async bridge | `model_tools.py` coerce_tool_args + _run_async |
| V5 | pre/post/transform hooks | `hermes_cli/plugins.py` invoke_hook |

---

## 总结

nano_hermes_agent V5 从源项目 ~500K 行代码中提取了工具系统的 6 个核心设计模式：

1. **自注册 + dispatch** — 工具 import 时自动注册，统一入口分发
2. **Toolsets + check_fn** — 按场景分组，运行时探测可用性
3. **Generation + TTL 两层缓存** — 注册表变化 O(1) 感知，外部状态定时刷新
4. **MCP 协议集成** — 后台 event loop + stdio 子进程，按需连接外部工具
5. **类型 coerce + async bridge** — dispatch 层统一修正类型、桥接异步
6. **Plugin hooks 生命周期** — pre/post/transform 三层钩子，load/unload 对称

省略的是生产环境的复杂性：多平台网关、会话持久化、技能系统、配置管理、安全防护、可观测性等。这些在理解核心模式后可以按需添加。
