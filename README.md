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
| `v10` | 外部 Provider（HTTP） | RemoteSemanticProvider + Mock 服务（FastAPI） |

## 快速开始

```bash
# 克隆并切到最新版本（V10）
git clone <repo-url>
git checkout v10

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

## 演进脉络（V6 → V10）

每个版本只解决前一版暴露的**一个问题**，避免一次性把所有抽象端上来。

### V6 — 文件记忆工具

**问题**：Agent 没有记忆，每次对话从零开始，无法记住用户偏好、项目上下文、过往纠正。

**解法**：`tools/memory_tool.py` 提供文件持久化的 `memory` tool。

| 特性 | 设计 |
|------|------|
| 存储格式 | 纯文本，`§` 分隔条目 |
| 持久化路径 | `~/.hermes/nano_memory/MEMORY.md` |
| 容量限制 | 2200 字符（防 system prompt 膨胀） |
| 定位方式 | 子串匹配（replace/remove 时） |

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

**问题**：V6 的记忆逻辑写死在 tool 里，要换后端（语义搜索、知识图谱）就得改散落各处的代码。"工具接口、存储逻辑、生命周期"三个关注点纠缠在一起。

**解法**：抽出 `memory/provider.py` 接口，把存储引擎和工具壳分开。

| 文件 | 职责 |
|------|------|
| `memory/provider.py` | `MemoryProvider` ABC——定义契约 |
| `memory/builtin.py` | `BuiltinMemoryProvider`——把 MemoryStore 包成 provider |
| `tools/memory_store.py` | 纯存储引擎（不再是 tool，不自注册） |

agent.py 不再直接持有 store，而是通过 provider 收 schema、路由 tool 调用。`memory` 工具不再走 registry 自注册，由 provider 管理。

### V8 — MemoryManager 编排器

**问题**：V7 的 agent.py 直接调用单个 provider。要支持第二个 provider 就得复制"调用 → 捕获异常 → 合并结果"的逻辑；多个 provider 都暴露 tool 时谁分发？

**解法**：`memory/manager.py` 引入 `MemoryManager`，做四件事：

1. **单一集成点**——agent.py 只与 manager 对话，不感知 provider 数量
2. **工具路由**——`_tool_to_provider` 字典按 tool 名找到目标 provider
3. **错误隔离**——每个 provider 调用包 try/except，单个失败不阻塞其他
4. **一个外部 provider 限制**——防止 tool schema 膨胀和后端冲突

### V9 — Agent Loop 生命周期集成

**问题**：V8 的 manager 能管 provider 但记忆是静态的——外部 provider 没机会在每轮"对的时机"做事（召回、持久化）。生命周期时序应该写在主循环里固定下来。

**解法**：三层改动。

1. **Provider ABC 扩展**（`memory/provider.py`）三个默认 no-op 钩子：
   - `on_turn_start(turn, msg)`——每轮开始通知
   - `prefetch(query)`——为即将到来的一轮召回相关上下文
   - `sync_turn(user, assistant)`——持久化完成的对话
   - 内置 provider 不需要 override（它通过 `system_prompt_block` 一次性注入全部记忆），外部 provider 按需实现。

2. **Manager 广播**（`memory/manager.py`）三个方法 + 两个围栏辅助：
   - `on_turn_start_all` / `prefetch_all` / `sync_all` —— 错误隔离地广播
   - `prefetch_all` 内部把多 provider 的召回结果先 `sanitize_context`，再用**唯一一对** `<memory-context>` 围栏 + 系统注释包裹
   - `sanitize_context` 防御外部 provider 注入伪造的围栏标签和系统注释

3. **Agent 主循环时序**（`agent.py`）：
   ```
   用户输入 → on_turn_start_all
            → prefetch_all（召回内容拼到 user message 前面）
            → tool loop
            → sync_all（用原始 user 输入持久化，围栏不入库）
   ```

**为什么注入到 user message 而不是 system prompt？**召回内容每轮变化，放 system prompt 会让 OpenAI 前缀缓存全废。注入 user message 才是正确位置——保 system prompt 稳定，缓存命中率高。

**V9 的 trade-off**：内置 provider 的 prefetch/sync_turn 都是 no-op，钩子的实际消费者要等 V10 的外部 provider 验证。架构对不对，V10 才能下结论。

### V10 — 外部 Provider（HTTP 服务）

**问题**：V9 的钩子接好了，但只有内置 provider 用，而它基本 no-op 这些方法。架构对不对没人能下结论——要证明 V7 的 ABC 抽象 + V8 的 manager 路由 + V9 的生命周期钩子真正成立，得有一个**真正使用** prefetch/sync 的外部 provider。

**解法**：把"语义记忆"做成独立 HTTP 服务，provider 端走 httpx 调用。

为什么选 HTTP 边界？源项目 `plugins/memory/` 8 个 provider 里 5 个是 HTTP 客户端（mem0、honcho、supermemory、retaindb、openviking）。HTTP 边界比"plugin 内嵌 Python 类"更贴近生产形态——provider 与服务端独立演化，把 mock server 换成真后端时 provider **一行不改**。

| 文件 | 职责 |
|------|------|
| `memory/remote_semantic.py` | `RemoteSemanticProvider`——HTTP 客户端 |
| `scripts/mock_memory_server.py` | FastAPI mock 服务（教学用，进程内 dict + hash 假向量） |

**钩子到 HTTP 端点的 1:1 映射**：

| 钩子 | HTTP 调用 | 备注 |
|------|----------|------|
| `is_available()` | `GET /healthz` | 失败时 manager 跳过本 provider |
| `initialize(session_id)` | 无网络 | 缓存 base_url 和 session_id |
| `prefetch(query)` | `POST /recall` | 返回 hits 拼成文本注入 user message |
| `sync_turn(u, a)` | `POST /sync` | 同步阻塞 |
| `system_prompt_block()` | 无 | 固定一行 "Long-term memory available" |
| `get_tool_schemas()` | 无 | 返回 `[]`——召回是隐式的 |
| `shutdown()` | 关闭 httpx client | |

**为什么不暴露 tool？**与 builtin 形成对照：builtin 用 tool 让模型显式调用 add/replace/remove；remote_semantic 用 prefetch 钩子在每轮**自动召回**——证明"prefetch 钩子做事而不是 tool"是另一条合法路径。

**为什么不引入向量数据库？**V10 的目的是验证 V9 钩子的形状（prefetch 接收什么、返回什么；sync_turn 该在哪触发），不是真做语义搜索。引入 chromadb / qdrant / pgvector 会让 nano 从单进程变多服务系统，掩盖钩子的真实形态。Mock 服务用 hash-based 假 embedding（md5 → 16 维 float）就够了——同样文本得到同样向量、能跑余弦相似度，足以演示真实时序。

**接入方式**——环境变量开关：

```bash
# 不设：跑 V9 的行为（只有 builtin provider）
python agent.py

# 设：加挂 RemoteSemanticProvider
export MEMORY_SERVICE_URL=http://127.0.0.1:8765
python agent.py
```

**启动 mock 服务**（另开终端）：

```bash
uv pip install -e ".[mock-server]"  # 装 fastapi + uvicorn
python scripts/mock_memory_server.py
```

**V10 简化（vs 源项目）**：同步阻塞调用（无后台线程）、无熔断器、无重试、无认证。保留：完整 MemoryProvider 接口实现、HTTP 边界、生命周期钩子 1:1 映射端点。

**架构验证点**：把 mock server 换成 FastAPI + sentence-transformers + Postgres/pgvector，provider 端**一行不改**——这正是 V7 抽出 ABC 的最终验证。

## 项目结构（V10）

```
nano_hermes_agent/
├── agent.py                    # 主循环 + 生命周期时序 + 按 env 注册外部 provider
├── model_tools.py              # 外层缓存 + MCP 工具自动包含
├── toolsets.py                 # 工具组定义（不含 memory，由 provider 管）
├── memory/                     # V7 引入，V8/V9/V10 扩展
│   ├── __init__.py             # 导出 Manager / Provider / 围栏辅助 / RemoteSemanticProvider
│   ├── provider.py             # MemoryProvider ABC（V9 加生命周期钩子）
│   ├── builtin.py              # BuiltinMemoryProvider（文件后端）
│   ├── manager.py              # MemoryManager 编排 + 围栏
│   └── remote_semantic.py      # V10 RemoteSemanticProvider（HTTP 客户端）
├── scripts/
│   └── mock_memory_server.py   # V10 FastAPI mock 服务（dict + hash 假向量）
├── tools/
│   ├── __init__.py             # 自动发现 + load/unload 生命周期
│   ├── registry.py             # dispatch: coerce + async + hooks
│   ├── hooks.py                # HookManager
│   ├── coerce.py               # 类型强制转换
│   ├── mcp_client.py           # MCP 客户端
│   ├── memory_store.py         # V7 拆出来的纯存储引擎
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
| V8 | 完成 | 多 provider 无编排，无错误隔离 | MemoryManager 路由 + 围栏 |
| V9 | 完成 | 没有"何时召回 / 何时持久化"的时序 | prefetch/sync 生命周期钩子 |
| V10 | 完成 | V9 钩子无外部消费者，架构未验证 | RemoteSemanticProvider + Mock 服务 |

