# Nano Hermes Agent — Memory System

在 V5（工具系统完整版）基础上，从零构建 AI Agent 记忆系统。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v5` | 基线 | 工具系统完整版（插件钩子） |
| `v6` | 文件记忆工具 | MemoryStore + 冻结快照 + system prompt 注入 |
| `v7` | MemoryProvider ABC | 接口抽象 + BuiltinMemoryProvider |
| `v8` | MemoryManager 编排器 | 路由 + 上下文围栏 + 错误隔离 |
| `v9` | 生命周期集成 | prefetch/sync 接入 agent loop |
| `v10` | 外部 Provider 插件 | MockSemanticProvider 验证架构 |

## 快速开始

```bash
# 克隆并切到当前版本（V7）
git clone <repo-url>
git checkout v7

# 初始化环境
uv venv .venv --python 3.11
uv pip install -e "."

# 配置 API
cp .env.example .env

# 运行
source .venv/bin/activate
python agent.py
```

## 演进脉络（V6 → V7）

每个版本只解决前一版暴露的**一个问题**。

### V6 — 文件记忆工具

**问题**：Agent 没有记忆，每次对话从零开始，无法记住用户偏好、项目上下文、过往纠正。

**解法**：`tools/memory_tool.py` 提供文件持久化的 `memory` tool，用 `§` 分隔条目存到 `~/.hermes/nano_memory/MEMORY.md`，2200 字符上限防 system prompt 膨胀。

**冻结快照模式**——双状态设计：
```
加载时 → _snapshot（冻结）→ 注入 system prompt（不变）
         _entries（实时）→ tool 响应反映当前状态
```
为什么冻结？system prompt 不变 = 前缀缓存命中率高 = 推理成本低。

memory tool 操作：

| 操作 | 参数 | 说明 |
|------|------|------|
| `add` | content | 添加一条记忆 |
| `replace` | old_text, content | 替换包含 old_text 的条目 |
| `remove` | old_text | 移除包含 old_text 的条目 |

### V7 — MemoryProvider ABC

**问题**：V6 的记忆逻辑写死在 tool 里，要换后端（语义搜索、知识图谱）就得改散落各处的代码——"工具接口、存储逻辑、生命周期"三个关注点纠缠在一起。

**解法**：抽出 `memory/provider.py` 接口，把存储引擎和工具壳分开。

| 文件 | 职责 |
|------|------|
| `memory/provider.py` | `MemoryProvider` ABC——定义契约 |
| `memory/builtin.py` | `BuiltinMemoryProvider`——把 MemoryStore 包成 provider |
| `tools/memory_store.py` | 纯存储引擎（不再是 tool，不自注册） |

`MemoryProvider` 契约的七个方法：`name` / `is_available` / `initialize` / `get_tool_schemas` / `handle_tool_call` / `system_prompt_block` / `shutdown`。

agent.py 不再直接持有 store，而是通过 provider 收 tool schema、路由 tool 调用、注入 system prompt。`memory` 工具不再走 registry 自注册，由 provider 管理；toolsets.py 里也移除了 `memory`。

为什么这一步重要：抽象层只在**有真实使用方**时才有意义。V7 只有一个 BuiltinMemoryProvider，看似 over-engineering，但它给 V8（编排多 provider）和 V10（外部插件 provider）铺好了基础——V8/V10 不需要再改 V7 的接口，只需新增实现。

## 项目结构（V7）

```
nano_hermes_agent/
├── agent.py                # 主循环 + provider 路由 + /memory 命令
├── model_tools.py          # 外层缓存 + MCP 工具自动包含
├── toolsets.py             # 工具组定义（不含 memory，由 provider 管）
├── memory/                 # V7 引入
│   ├── __init__.py         # 导出 MemoryProvider / BuiltinMemoryProvider
│   ├── provider.py         # ABC 接口
│   └── builtin.py          # 内置 provider 实现
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

Agent > 已记住你的偏好。下次我会优先推荐 vim 风格的配置。

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
| V8 | 待实现 | 多 provider 无编排，无错误隔离 | MemoryManager 路由 + 围栏 |
| V9 | 待实现 | 没有"何时召回 / 何时持久化"的时序 | prefetch/sync 生命周期钩子 |
| V10 | 待实现 | V9 钩子无外部消费者，架构未验证 | MockSemanticProvider 插件 |
