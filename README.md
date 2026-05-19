# Nano Hermes Agent — Memory System

在 V5（工具系统完整版）基础上，从零构建 AI Agent 记忆系统。每个 git 分支对应一个迭代版本。

## 前置条件

本项目假设你已完成工具系统的迭代（V0-V5），拥有完整的工具注册表、toolset 分组、缓存、类型修复、异步桥接、MCP 动态加载和插件钩子系统。

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
# 克隆并切到 v6 分支
git clone <repo-url>
git checkout v6

# 初始化环境
uv venv .venv --python 3.11
uv pip install -e "."

# 配置 API
cp .env.example .env
# 编辑 .env 填入你的 API key

# 运行
source .venv/bin/activate
python agent.py
```

## 当前版本：V6 — 文件记忆工具

让 Agent 拥有跨会话记忆能力。核心设计：文件持久化 + 冻结快照 + tool 实时响应。

```
nano_hermes_agent/
├── agent.py              # 主循环 + 记忆注入 + /memory 命令
├── model_tools.py        # 外层缓存 + MCP 工具自动包含
├── toolsets.py           # 工具组定义（含 memory）
├── tools/
│   ├── __init__.py       # 自动发现 + load/unload 生命周期
│   ├── registry.py       # dispatch: coerce + async + hooks
│   ├── hooks.py          # HookManager
│   ├── coerce.py         # 类型强制转换
│   ├── mcp_client.py     # MCP 客户端
│   ├── memory_tool.py    # V6 新增：MemoryStore + tool handler
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

### V6 解决了什么问题？

Agent 没有记忆 — 每次对话都从零开始，无法记住用户偏好、项目上下文、之前的纠正。

### 核心设计

**MemoryStore** — 文件持久化引擎：

| 特性 | 设计 |
|------|------|
| 存储格式 | 纯文本，`§` 分隔条目 |
| 持久化路径 | `~/.hermes/nano_memory/MEMORY.md` |
| 容量限制 | 2200 字符（防止 system prompt 膨胀） |
| 定位方式 | 子串匹配（replace/remove 时） |

**冻结快照模式** — 双状态设计：

```
加载时 → _snapshot（冻结）→ 注入 system prompt（不变）
         _entries（实时）→ tool 响应反映当前状态
```

为什么冻结？system prompt 不变 = 前缀缓存命中率高 = 推理成本低。

### memory tool

| 操作 | 参数 | 说明 |
|------|------|------|
| `add` | content | 添加一条记忆 |
| `replace` | old_text, content | 替换包含 old_text 的条目 |
| `remove` | old_text | 移除包含 old_text 的条目 |

### 使用示例

```
You > 我喜欢用 vim 风格的快捷键
  [tool] memory({"action": "add", "content": "用户偏好：vim 风格快捷键"})
  [result] (ok)

Agent > 已记住你的偏好。下次我会优先推荐 vim 风格的配置。

You > /memory
  [memory] 1 entries, 28/2200 chars
    1. 用户偏好：vim 风格快捷键
```

### 记忆命令

| 命令 | 说明 |
|------|------|
| `/memory` | 查看当前记忆条目和用量 |

### 迭代路线

| 版本 | 问题 | 解法 |
|------|------|------|
| V6 | Agent 没有记忆 | MemoryStore + memory tool |
| V7 | 记忆逻辑写死，无法替换后端 | MemoryProvider ABC 接口抽象 |
| V8 | 多 provider 无编排，无错误隔离 | MemoryManager 路由 + 围栏 |
| V9 | 记忆加载阻塞首轮响应 | prefetch/sync 生命周期钩子 |
| V10 | 无法验证架构对外部后端的支持 | MockSemanticProvider 插件 |
