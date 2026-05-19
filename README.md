# Nano Hermes Agent — Memory System

在 V5（工具系统完整版）基础上，从零构建 AI Agent 记忆系统。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v5` | 基线 | 工具系统完整版（插件钩子） |
| `v6` | 文件记忆工具 | MemoryStore + 冻结快照 + system prompt 注入 |
| `v7` | MemoryProvider ABC | 接口抽象 + BuiltinMemoryProvider |
| `v8` | MemoryManager 编排器 | 路由 + 错误隔离 + 外部 provider 限制 |
| `v9` | 生命周期集成 | prefetch/sync 接入 agent loop |
| `v10` | 外部 Provider 插件 | MockSemanticProvider 验证架构 |

## 快速开始

```bash
# 克隆并切到当前版本（V8）
git clone <repo-url>
git checkout v8

# 初始化环境
uv venv .venv --python 3.11
uv pip install -e "."

# 配置 API
cp .env.example .env

# 运行
source .venv/bin/activate
python agent.py
```

## 演进脉络（V6 → V8）

每个版本只解决前一版暴露的**一个问题**。

### V6 — 文件记忆工具

**问题**：Agent 没有记忆，每次对话从零开始。

**解法**：`memory` tool + `tools/memory_store.py` 文件持久化（`~/.hermes/nano_memory/MEMORY.md`，`§` 分隔，2200 字符上限）。

**冻结快照模式**——双状态设计：
```
加载时 → _snapshot（冻结）→ 注入 system prompt（不变）
         _entries（实时）→ tool 响应反映当前状态
```
为什么冻结？system prompt 不变 = 前缀缓存命中率高 = 推理成本低。

memory tool 三个操作：`add` / `replace` / `remove`，定位通过子串匹配。

### V7 — MemoryProvider ABC

**问题**：V6 的记忆逻辑写死在 tool 里，要换后端就得改散落各处的代码——"工具接口、存储逻辑、生命周期"三个关注点纠缠。

**解法**：抽出 `memory/provider.py` 接口，把存储引擎和工具壳分开。

| 文件 | 职责 |
|------|------|
| `memory/provider.py` | `MemoryProvider` ABC——定义契约 |
| `memory/builtin.py` | `BuiltinMemoryProvider`——把 MemoryStore 包成 provider |
| `tools/memory_store.py` | 纯存储引擎（不再是 tool，不自注册） |

agent.py 通过 provider 收 schema、路由 tool；toolsets.py 移除 `memory`（由 provider 管理）。

### V8 — MemoryManager 编排器

**问题**：V7 的 agent.py 直接调用单个 provider。要支持第二个 provider 就得复制"调用 → 捕获异常 → 合并结果"的逻辑；多个 provider 都暴露 tool 时谁分发？

**解法**：`memory/manager.py` 引入 `MemoryManager`，做四件事：

1. **单一集成点**——agent.py 只与 manager 对话，不感知 provider 数量
2. **工具路由**——`_tool_to_provider` 字典按 tool 名找到目标 provider
3. **错误隔离**——每个 provider 调用包 try/except，单个失败不阻塞其他
4. **一个外部 provider 限制**——`add_provider` 只接受一个 `name != "builtin"` 的 provider，第二个被 reject 并打 warning（防 tool schema 膨胀和后端冲突）

manager 提供的接口：

| 方法 | 用途 |
|------|------|
| `add_provider(p)` | 注册（含外部限制） |
| `get_all_tool_schemas()` / `get_all_tool_names()` | 收集所有 provider 暴露的工具 |
| `has_tool(name)` / `handle_tool_call(name, args)` | 工具路由 |
| `build_system_prompt()` | 拼接所有 provider 的 system prompt block |
| `initialize_all()` / `shutdown_all()` | 生命周期入口 |

V8 故意没有：
- `prefetch_all` / `sync_all`——留给 V9 的生命周期版本
- `sanitize_context` / `build_memory_context_block`——围栏辅助等 V9 prefetch 真正调用时才加

按 nano 方法论"每版本一个问题"，没有调用方的代码不进 diff。

## 项目结构（V8）

```
nano_hermes_agent/
├── agent.py                # 主循环 + manager 路由 + /memory 命令
├── model_tools.py          # 外层缓存 + MCP 工具自动包含
├── toolsets.py             # 工具组定义（不含 memory，由 provider 管）
├── memory/                 # V7 引入，V8 扩展
│   ├── __init__.py         # 导出 MemoryProvider / BuiltinMemoryProvider / MemoryManager
│   ├── provider.py         # ABC 接口
│   ├── builtin.py          # 内置 provider 实现
│   └── manager.py          # V8 新增：编排器
├── tools/
│   ├── __init__.py         # 自动发现 + load/unload 生命周期
│   ├── registry.py         # dispatch: coerce + async + hooks
│   ├── hooks.py            # HookManager
│   ├── coerce.py           # 类型强制转换
│   ├── mcp_client.py       # MCP 客户端
│   ├── memory_store.py     # V7 拆出来的纯存储引擎
│   ├── terminal_tool.py
│   ├── read_file_tool.py
│   ├── async_demo_tool.py
│   └── docker_exec_tool.py
├── plugins/
│   ├── write_file_tool.py
│   ├── logging_hook.py
│   ├── truncate_hook.py
│   └── rate_limit_hook.py
├── pyproject.toml
├── .env.example
└── .gitignore
```

## 使用示例

```
You > 我喜欢用 vim 风格的快捷键
  [tool] memory({"action": "add", "content": "用户偏好：vim 风格快捷键"})
  [result] (ok)

Agent > 已记住你的偏好。

You > /memory
  [memory] 1 entries, 28/2200 chars
    1. 用户偏好：vim 风格快捷键
```

## 内置命令

| 命令 | 说明 |
|------|------|
| `/memory` | 查看当前记忆条目和用量 |
| `/tools` | 查看当前可用工具 |
| `/load <file>` | 运行时加载 plugins/ 下的工具 |
| `/plugin list/load/unload` | 插件生命周期管理 |
| `/mcp connect/disconnect/refresh` | MCP server 管理 |

## 迭代路线

| 版本 | 状态 | 问题 | 解法 |
|------|------|------|------|
| V6 | 完成 | Agent 没有记忆 | MemoryStore + memory tool |
| V7 | 完成 | 记忆逻辑写死，无法替换后端 | MemoryProvider ABC 接口抽象 |
| V8 | 完成 | 多 provider 无编排，无错误隔离 | MemoryManager 路由 + 隔离 |
| V9 | 待实现 | 没有"何时召回 / 何时持久化"的时序 | prefetch/sync 生命周期钩子 |
| V10 | 待实现 | V9 钩子无外部消费者，架构未验证 | MockSemanticProvider 插件 |
