# V5 与源项目（hermes-agent）的差异对比

## 概述

nano_hermes_agent V5 是源项目 hermes-agent 插件钩子系统的教学简化版。
保留了核心设计模式，去掉了生产环境的复杂性。

---

## 钩子系统对比

| 维度 | 源项目 (hermes-agent) | V5 (nano) |
|------|----------------------|-----------|
| 钩子数量 | 15+ 种（pre/post_tool_call, pre/post_llm_call, transform_*, on_session_*, subagent_stop 等） | 3 种（pre_tool_call, post_tool_call, transform_tool_result） |
| 管理器 | `PluginManager` 类（~1200 行） | `HookManager` 类（~40 行） |
| 注册方式 | `ctx.register_hook(name, callback)` 通过 PluginContext 门面 | `hook_manager.register(name, callback)` 直接调用 |
| 调用方式 | `invoke_hook(hook_name, **kwargs)` 模块级函数 | `hook_manager.invoke(hook_name, **kwargs)` |
| 错误隔离 | 每个回调 try/except，日志记录 | 相同 |
| 返回值语义 | 与 V5 相同（pre 可 block，transform 第一个非 None 替换） | 相同 |

## 插件系统对比

| 维度 | 源项目 | V5 |
|------|--------|-----|
| 插件发现 | 4 个来源：bundled、user (~/.hermes/plugins/)、project (.hermes/plugins/)、pip entry-point | 1 个来源：`plugins/` 目录 |
| 插件清单 | `plugin.yaml` 声明 name、kind、version、dependencies | 无清单，约定导出 `register()` / `deregister()` |
| 插件种类 | 5 种 kind：standalone、backend、exclusive、platform、model-provider | 无分类，所有插件等价 |
| 生命周期 | load → register(ctx) → hooks fire → unload | load → register(hook_manager) → hooks fire → deregister → unload |
| 启用方式 | `config.yaml` 中 `plugins.enabled` 列表 | `/plugin load <file>` 命令 |
| 卸载 | 支持（从 config 移除或 CLI 命令） | `/plugin unload <file>` |
| PluginContext | 提供 register_hook、register_tool、register_command、get_config 等门面方法 | 无，直接传 hook_manager |

## dispatch 流程对比

### 源项目 (`model_tools.py` handle_function_call)

```
handle_function_call(tool_name, args):
    1. coerce_tool_args(tool_name, args)          # 类型修正
    2. get_pre_tool_call_block_message()           # pre hook（可 block）
       └─ invoke_hook("pre_tool_call", ...)
    3. registry.dispatch(tool_name, args)          # 执行（内部处理 is_async）
    4. invoke_hook("post_tool_call", ...)          # post hook
    5. invoke_hook("transform_tool_result", ...)   # transform hook
    6. 结果截断（max_result_size_chars）
    7. 返回
```

### V5 (`registry.py` dispatch)

```
dispatch(name, args):
    1. coerce_args(schema, args)                   # 类型修正
    2. hook_manager.invoke("pre_tool_call", ...)   # pre hook（可 block）
    3. handler(coerced) 或 _run_async(...)         # 执行（内部处理 is_async）
    4. hook_manager.invoke("post_tool_call", ...)  # post hook
    5. hook_manager.invoke("transform_tool_result", ...)  # transform hook
    6. 返回
```

**关键差异：** 源项目的 coerce 和 hooks 分布在 `model_tools.py`（外层）和 `registry.py`（内层）两个文件中。V5 把所有逻辑集中在 `registry.dispatch()` 一个方法里，更容易理解。

## 源项目有但 V5 省略的功能

| 功能 | 源项目实现 | V5 省略原因 |
|------|-----------|-------------|
| `max_result_size_chars` | ToolEntry 字段，dispatch 后自动截断 | 用 truncate_hook 插件演示同等效果 |
| `dynamic_schema_overrides` | 运行时动态修改工具 schema | 教程不需要 |
| `pre_approval_request` | 危险操作前请求用户确认 | 教程不涉及安全审批 |
| `on_session_start/end` | 会话生命周期钩子 | 教程无会话持久化 |
| `pre/post_llm_call` | LLM 调用前后钩子 | 教程聚焦工具调用 |
| `transform_llm_output` | 修改 LLM 输出 | 同上 |
| `subagent_stop` | 子 agent 停止通知 | 教程无子 agent |
| 并行工具执行 | ThreadPoolExecutor 并行 dispatch | 教程串行执行 |
| Circuit breaker | MCP 工具连续失败后熔断 | 教程不需要容错 |
| 插件依赖解析 | plugin.yaml 声明 dependencies | 教程插件无依赖 |

## 源项目有但 V5 简化的功能

| 功能 | 源项目 | V5 简化版 |
|------|--------|-----------|
| 异步桥接 | 3 种策略（main thread / worker thread / async context），持久 loop | 2 种策略（asyncio.run / thread pool），无持久 loop |
| MCP 连接 | 重连、熔断、RPC lock、interrupt 支持 | 基本 connect/disconnect/refresh |
| 类型 coerce | 处理 nullable、array-wrapping、nested object | 只处理基本类型转换 |
| 插件配置 | 每个插件可有独立 config section | 无配置 |

## 设计哲学差异

| | 源项目 | V5 |
|---|--------|-----|
| 目标 | 生产可用，支持多平台网关 | 教学演示，理解核心模式 |
| 代码量 | `plugins.py` ~1400 行，`model_tools.py` ~900 行 | `hooks.py` ~60 行，dispatch 改动 ~20 行 |
| 错误处理 | 完善（超时、重试、熔断、降级） | 最小（try/except 隔离） |
| 配置 | YAML 文件 + 环境变量 + CLI 参数 | 无配置文件，命令行交互 |
| 测试 | 完整单元测试 + 集成测试 | 手动验证 |

## 总结

V5 保留了源项目钩子系统的三个核心设计决策：

1. **钩子语义分层** — pre 可阻止、post 只观察、transform 可替换
2. **错误隔离** — 一个插件出错不影响其他插件和核心流程
3. **显式生命周期** — register/deregister 对称，支持运行时 load/unload

去掉的是生产环境的复杂性：多来源发现、YAML 清单、依赖解析、并行执行、熔断重试等。这些在理解核心模式后可以逐步添加。
