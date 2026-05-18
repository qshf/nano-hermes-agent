# Nano Hermes Agent

从零搭建 AI Agent 工具系统的教程仓库。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v0` | 最小可用 | 无注册表，if/elif 分发 |
| `v1` | 注册表 | 自注册 + dispatch |
| `v2` | Toolsets | 名字展开 + check_fn |
| `v3` | 缓存 + MCP | generation + TTL + MCP 动态加载 |
| `v4` | 类型修复 | coerce + 异步桥接（计划中） |
| `v5` | 插件钩子 | pre/post/transform（计划中） |

## 快速开始

```bash
# 克隆并切到 v3 分支
git clone <repo-url>
git checkout v3

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

## 当前版本：V3 — 缓存层 + MCP 集成

引入两层缓存解决性能问题，同时添加 MCP 协议支持实现按需动态加载外部工具。

```
nano_hermes_agent/
├── agent.py              # 主循环 + /mcp /load /tools 命令
├── model_tools.py        # 外层缓存 + MCP 工具自动包含
├── toolsets.py           # 工具组定义
├── mcp_server_demo.py    # 示例 MCP server（FastMCP，stdio）
├── tools/
│   ├── __init__.py       # 自动发现 + load_plugin()
│   ├── registry.py       # _generation + check_fn TTL 缓存
│   ├── mcp_client.py     # MCP 客户端：connect/discover/call/refresh
│   ├── terminal_tool.py
│   ├── read_file_tool.py
│   └── docker_exec_tool.py
├── plugins/
│   └── write_file_tool.py  # 动态加载演示
├── pyproject.toml
├── .env.example
└── .gitignore
```

### V3 解决了 V2 的什么问题？

| V2 的问题 | V3 的解法 |
|-----------|-----------|
| 每轮都遍历注册表 + 调 check_fn | 外层缓存命中时 ~0μs 返回 |
| check_fn 可能 fork 进程，每轮都调 | 30s TTL 缓存，过期才重新执行 |
| 无法感知注册表是否变化 | `_generation` 计数器，O(1) 比较 |
| 工具只能静态加载 | MCP 协议支持按需连接外部工具服务 |

### 两层缓存架构

```
Agent 每轮调用 get_tool_definitions(["core"])
        ↓
┌─ model_tools 外层缓存 ─────────────────────┐
│  cache_key = (enabled_toolsets, generation) │
│  命中 → 直接返回（~0μs）                    │
│  未命中 → 重算 ↓                            │
└─────────────────────────────────────────────┘
        ↓
┌─ registry 内层缓存 ─────────────────────────┐
│  check_fn 结果 → (timestamp, bool)          │
│  30s 内 → 返回缓存结果                       │
│  过期 → 重新执行 check_fn()                  │
└─────────────────────────────────────────────┘
```

### MCP 集成

通过 `/mcp` 命令按需连接 MCP server，避免全部加载导致上下文过长。

**使用方式：**

```
You > /mcp connect demo python mcp_server_demo.py
  [mcp] Connected to 'demo' (generation: 3 → 5)
  [mcp] Tools: mcp_demo_get_weather, mcp_demo_get_time

You > 北京今天天气怎么样？
  [tool] mcp_demo_get_weather({"city": "北京"})
  [result] (ok)

Agent > 北京今天天气晴，气温 22°C，湿度 45%。

You > /mcp disconnect demo
  [mcp] Disconnected 'demo'
```

**MCP 命令：**

| 命令 | 说明 |
|------|------|
| `/mcp` | 列出已连接的 MCP servers |
| `/mcp connect <name> <cmd> [args...]` | 连接 MCP server |
| `/mcp disconnect <name>` | 断开连接 |
| `/mcp refresh <name>` | 刷新工具列表 |

**什么时候用 `/mcp refresh`？**

`refresh` 会对当前已连接的 MCP server 重新执行 `list_tools()`，然后把旧工具从 registry 注销，再按最新列表重新注册。

适合用 `refresh` 的情况：

- MCP server 进程还在运行
- 工具是否可用取决于运行时环境
- 这个环境变化能被当前进程实时感知

例如：

```
# 连接时 Docker 没启动，相关工具不可用
# 后来启动了 Docker
You > /mcp refresh demo
```

不适合只用 `refresh` 的情况：

- 修改了 MCP server 的 Python 源码
- 新增了 `@mcp.tool()` 函数
- 安装了新的 Python import 包依赖
- 改了启动参数或环境变量，而当前子进程无法自动感知

这种情况建议重启 MCP server：

```
You > /mcp disconnect demo
You > /mcp connect demo python mcp_server_demo.py
```

简单规则：

```
外部运行状态变了，进程能实时看到 → /mcp refresh <name>
代码、Python 包、启动参数变了 → disconnect + connect
```

**工作原理：**

```
/mcp connect demo python mcp_server_demo.py
        ↓
┌─ mcp_client.py ─────────────────────────────┐
│  1. _ensure_mcp_loop() → 后台 daemon 线程    │
│  2. stdio_client() → 子进程启动 MCP server   │
│  3. session.list_tools() → 发现工具          │
│  4. registry.register() → generation 递增    │
└─────────────────────────────────────────────┘
        ↓
下一轮对话 get_tool_definitions()
  → cache_key 中 generation 变了
  → 缓存失效 → 重算 → MCP 工具出现在列表中
```

### 动态加载（plugins）

```
You > /load write_file_tool.py
  [loaded] write_file_tool.py (generation: 3 → 4)
```

### V3 的问题（V4 要解决的）

1. LLM 返回的参数类型经常不对（`"42"` 而非 `42`，`"true"` 而非 `true`）
2. async handler 在 sync 上下文无法执行
3. 没有类型强制转换机制
