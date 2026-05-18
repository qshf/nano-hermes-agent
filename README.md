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

## 当前版本：V4 — 类型修复 + 异步桥接

在 dispatch 层统一解决 LLM 参数类型错误和 async handler 执行问题。

```
nano_hermes_agent/
├── agent.py              # 主循环 + /mcp /load /tools 命令
├── model_tools.py        # 外层缓存 + MCP 工具自动包含
├── toolsets.py           # 工具组定义
├── mcp_server_demo.py    # 示例 MCP server（FastMCP，stdio）
├── tools/
│   ├── __init__.py       # 自动发现 + load_plugin()
│   ├── registry.py       # dispatch: coerce + is_async 桥接
│   ├── coerce.py         # V4 新增：类型强制转换
│   ├── mcp_client.py     # MCP 客户端
│   ├── terminal_tool.py
│   ├── read_file_tool.py
│   ├── async_demo_tool.py  # V4 新增：异步工具演示
│   └── docker_exec_tool.py
├── plugins/
│   └── write_file_tool.py
├── pyproject.toml
├── .env.example
└── .gitignore
```

### V4 解决了 V3 的什么问题？

| V3 的问题 | V4 的解法 |
|-----------|-----------|
| LLM 返回 `"42"` 而非 `42` | dispatch 前根据 schema 自动 coerce |
| LLM 返回 `"true"` 而非 `true` | `_coerce_bool("true")` → `True` |
| 每个 handler 各自做类型转换 | 集中在 `coerce_args()` 一处处理 |
| async handler 无法在 sync 循环执行 | `is_async=True` + `_run_async()` 自动桥接 |

### 类型强制转换（coerce）

```
LLM 返回: {"command": "ls", "timeout": "30"}
                                        ↑ schema 声明 "type": "integer"
        ↓ coerce_args()
Handler 收到: {"command": "ls", "timeout": 30}
                                          ↑ 已转为 int
```

**支持的转换：**

| Schema 类型 | 输入 | 输出 |
|-------------|------|------|
| `integer` | `"42"` | `42` |
| `number` | `"3.14"` | `3.14` |
| `boolean` | `"true"` | `True` |
| `array` | `"[1,2,3]"` | `[1, 2, 3]` |
| `object` | `'{"a":1}'` | `{"a": 1}` |

**安全原则：** 转换失败时保留原值，不抛异常。Handler 仍然可以自行处理。

### 异步桥接（async bridge）

```python
# 注册时声明 is_async=True
async def my_handler(args: dict) -> str:
    result = await some_async_api(args["query"])
    return json.dumps({"output": result})

registry.register(schema, my_handler, is_async=True)
```

**dispatch 内部流程：**

```
registry.dispatch("async_demo", {"seconds": "2"})
        ↓
1. coerce_args() → {"seconds": 2}     # 先修正类型
        ↓
2. entry["is_async"] == True?
   → _run_async(handler(coerced_args))  # 桥接到 async
        ↓
3. _run_async 策略：
   - 无 running loop → asyncio.run()
   - 有 running loop → 开新线程跑 asyncio.run()
```

**对比 MCP 工具的异步方案：**

| | MCP 工具 | V4 async 工具 |
|---|----------|---------------|
| 场景 | 长连接（session 持续存在） | 一次性协程（用完即走） |
| 机制 | 专用后台 event loop + `run_coroutine_threadsafe` | `_run_async()` 按需创建/复用 loop |
| 注册 | `is_async=False`（handler 内部自己桥接） | `is_async=True`（dispatch 自动桥接） |
| 复杂度 | 高（需要管理 loop 生命周期） | 低（~15 行代码） |

### V4 的问题（V5 要解决的）

1. 工具调用没有 pre/post 钩子（无法统一做日志、限流、重试）
2. 工具结果没有 transform 机制（无法统一截断、格式化）
3. 没有插件生命周期管理（load 了就不能 unload）
