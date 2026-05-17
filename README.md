# Nano Hermes Agent

从零搭建 AI Agent 工具系统的教程仓库。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v0` | 最小可用 | 无注册表，if/elif 分发 |
| `v1` | 注册表 | 自注册 + dispatch |
| `v2` | Toolsets | 名字展开 + check_fn（计划中） |
| `v3` | 缓存 | generation + TTL（计划中） |
| `v4` | 类型修复 | coerce + 异步桥接（计划中） |
| `v5` | 插件钩子 | pre/post/transform（计划中） |

## 快速开始

```bash
# 克隆并切到 v1 分支
git clone <repo-url>
git checkout v1

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

## 当前版本：V1 — 注册表

引入 `ToolRegistry`，工具通过 `register()` 自注册，agent.py 不再需要手动管理工具。

```
nano_hermes_agent/
├── agent.py              # 主循环：只需 import tools 即可获得所有工具
├── tools/
│   ├── __init__.py       # 自动发现：扫描 *_tool.py 并 import
│   ├── registry.py       # ToolRegistry：register() + dispatch() + get_openai_tools()
│   ├── terminal_tool.py  # schema + handler + 自注册
│   └── read_file_tool.py # schema + handler + 自注册
├── pyproject.toml
├── .env.example
└── .gitignore
```

### V1 解决了 V0 的什么问题？

| V0 的问题 | V1 的解法 |
|-----------|-----------|
| 新增工具要改 agent.py 三处 | 新增工具只需创建 `*_tool.py`，零改动 agent.py |
| schema 和 handler 绑定是隐式的 | `register(schema, handler)` 显式绑定 |
| 10 个工具时 agent.py 变成一坨 import | `__init__.py` 自动发现，agent.py 只有一行 `import tools` |
| 没有运行时判断工具可用性 | V2 将通过 `check_fn` 解决 |

### 新增工具只需一步

创建 `tools/my_tool.py`：

```python
from tools.registry import registry

MY_SCHEMA = {
    "name": "my_tool",
    "description": "...",
    "parameters": { ... },
}

def my_handler(args: dict) -> str:
    ...

registry.register(MY_SCHEMA, my_handler)
```

无需修改任何其他文件，agent 下次启动时自动发现。

### V1 的问题（V2 要解决的）

1. 所有已注册工具都会发给 LLM，无法按场景分组
2. 没有"工具当前能不能用"的运行时判断（如 Docker 未安装时隐藏 docker 工具）
3. 工具列表是静态的，无法根据上下文动态调整
