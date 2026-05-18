# Nano Hermes Agent

从零搭建 AI Agent 工具系统的教程仓库。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v0` | 最小可用 | 无注册表，if/elif 分发 |
| `v1` | 注册表 | 自注册 + dispatch |
| `v2` | Toolsets | 名字展开 + check_fn |
| `v3` | 缓存 + MCP | generation + TTL + MCP 动态加载 |
| `v4` | 类型修复 | coerce + 异步桥接 |
| `v5` | 插件钩子 | pre/post/transform |

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

## 当前版本：V5 — 插件钩子系统

在 dispatch 流程中插入三个钩子点，并增强插件系统支持 load/unload 生命周期。

```
nano_hermes_agent/
├── agent.py              # 主循环 + /plugin /mcp /load /tools 命令
├── model_tools.py        # 外层缓存 + MCP 工具自动包含
├── toolsets.py           # 工具组定义
├── mcp_server_demo.py    # 示例 MCP server（FastMCP，stdio）
├── tools/
│   ├── __init__.py       # 自动发现 + load/unload 生命周期
│   ├── registry.py       # dispatch: coerce + async + hooks
│   ├── hooks.py          # V5 新增：HookManager
│   ├── coerce.py         # 类型强制转换
│   ├── mcp_client.py     # MCP 客户端
│   ├── terminal_tool.py
│   ├── read_file_tool.py
│   ├── async_demo_tool.py
│   └── docker_exec_tool.py
├── plugins/
│   ├── write_file_tool.py    # 动态工具加载演示
│   ├── logging_hook.py       # V5：日志钩子
│   ├── truncate_hook.py      # V5：截断钩子
│   └── rate_limit_hook.py    # V5：限流钩子
├── pyproject.toml
├── .env.example
└── .gitignore
```

### V5 解决了 V4 的什么问题？

| V4 的问题 | V5 的解法 |
|-----------|-----------|
| 无法统一做调用日志 | `post_tool_call` 钩子观察每次调用 |
| 无法统一限流/权限检查 | `pre_tool_call` 钩子可阻止执行 |
| 无法统一截断/格式化结果 | `transform_tool_result` 钩子替换结果 |
| 插件 load 了不能 unload | `unload_plugin()` + `deregister()` |

### 三种钩子

| 钩子 | 时机 | 语义 | 返回值 |
|------|------|------|--------|
| `pre_tool_call` | handler 执行前 | 可阻止 | `{"action": "block", "message": "..."}` |
| `post_tool_call` | handler 执行后 | 观察者 | 忽略 |
| `transform_tool_result` | post 之后 | 可替换 | 第一个非 None 字符串替换结果 |

### dispatch 流程（V5 完整版）

```
registry.dispatch("terminal", {"command": "ls", "timeout": "30"})
        ↓
1. coerce_args()                    # V4: "30" → 30
        ↓
2. pre_tool_call hooks              # V5: 可阻止（如限流）
   ├─ rate_limit → block?
   └─ logging → print "→ terminal(...)"
        ↓
3. handler(coerced_args)            # 执行工具（计时）
        ↓
4. post_tool_call hooks             # V5: 观察（如日志）
   └─ logging → print "← terminal (5ms)"
        ↓
5. transform_tool_result hooks      # V5: 可替换（如截断）
   └─ truncate → 超长则截断
        ↓
6. return result
```

### 插件生命周期

```python
# plugins/logging_hook.py

def pre_tool_call(tool_name, args, **kw):
    print(f"  [hook:log] → {tool_name}({args})")

def post_tool_call(tool_name, args, result, duration_ms, **kw):
    print(f"  [hook:log] ← {tool_name} ({duration_ms}ms)")

def register(hook_manager):
    hook_manager.register("pre_tool_call", pre_tool_call)
    hook_manager.register("post_tool_call", post_tool_call)

def deregister(hook_manager):
    hook_manager.deregister("pre_tool_call", pre_tool_call)
    hook_manager.deregister("post_tool_call", post_tool_call)
```

**使用方式：**

```
You > /plugin load logging_hook.py
  [plugin] Loaded 'logging_hook.py'
  [plugin] Hooks: pre_tool_call, post_tool_call

You > 列出当前目录的文件
  [hook:log] → terminal({'command': 'ls'})
  [hook:log] ← terminal (3ms) {"output": "..."}

Agent > 当前目录包含以下文件：...

You > /plugin unload logging_hook.py
  [plugin] Unloaded 'logging_hook.py'
```

**插件命令：**

| 命令 | 说明 |
|------|------|
| `/plugin` | 列出已加载的插件 |
| `/plugin load <file>` | 加载插件并注册钩子 |
| `/plugin unload <file>` | 注销钩子并卸载插件 |

### 示例插件

| 插件 | 钩子 | 功能 |
|------|------|------|
| `logging_hook.py` | pre + post | 打印调用入口/出口日志（含耗时） |
| `truncate_hook.py` | transform | 截断超过 2000 字符的结果 |
| `rate_limit_hook.py` | pre (block) | 每分钟最多 10 次调用 |
