# Nano Hermes Agent

从零搭建 AI Agent 工具系统的教程仓库。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v0` | 最小可用 | 无注册表，if/elif 分发 |
| `v1` | 注册表 | 自注册 + dispatch |
| `v2` | Toolsets | 名字展开 + check_fn |
| `v3` | 缓存 | generation + TTL（计划中） |
| `v4` | 类型修复 | coerce + 异步桥接（计划中） |
| `v5` | 插件钩子 | pre/post/transform（计划中） |

## 快速开始

```bash
# 克隆并切到 v2 分支
git clone <repo-url>
git checkout v2

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

## 当前版本：V2 — Toolsets + check_fn

引入工具分组和运行时可用性判断。Agent 不再关心具体工具名，只需指定 toolset。

```
nano_hermes_agent/
├── agent.py              # 主循环：指定 ENABLED_TOOLSETS，通过 model_tools 获取工具
├── model_tools.py        # 薄包装：串联 toolsets 展开 + registry 过滤
├── toolsets.py           # 工具组定义：toolset 名 → 工具名列表
├── tools/
│   ├── __init__.py       # 自动发现
│   ├── registry.py       # ToolRegistry + check_fn 过滤
│   ├── terminal_tool.py  # 自注册（无 check_fn，始终可用）
│   ├── read_file_tool.py # 自注册（无 check_fn，始终可用）
│   └── docker_exec_tool.py  # 自注册 + check_fn（Docker 未装则隐藏）
├── pyproject.toml
├── .env.example
└── .gitignore
```

### V2 解决了 V1 的什么问题？

| V1 的问题 | V2 的解法 |
|-----------|-----------|
| 所有工具都暴露给 LLM，无法分组 | `toolsets.py` 定义工具组，按场景启用 |
| 没有运行时可用性判断 | `check_fn` 返回 False 则自动隐藏 |
| agent 需要知道具体工具名 | 只需指定 toolset 名，自动展开 |

### 三层架构

```
Agent
  ↓ get_tool_definitions(["core"])
model_tools.py
  ↓ resolve_toolsets(["core"]) → ["terminal", "read_file"]
toolsets.py
  ↓ get_definitions(["terminal", "read_file"])
registry.py  ← check_fn 过滤
  ↓
最终 schema 列表
```

### check_fn 示例

```python
import shutil
from tools.registry import registry

def docker_available() -> bool:
    return shutil.which("docker") is not None

registry.register(SCHEMA, handler, check_fn=docker_available)
```

Docker 未安装时，`docker_exec` 不会出现在发给 LLM 的工具列表中。

### 新增 Toolset

编辑 `toolsets.py`：

```python
TOOLSETS = {
    "core": ["terminal", "read_file"],
    "docker": ["terminal", "read_file", "docker_exec"],
    "my_new_set": ["terminal", "read_file", "my_tool"],  # 新增
}
```

### V2 的问题（V3 要解决的）

1. 每轮对话都调 `check_fn`（可能 fork 进程探测），性能浪费
2. 每轮都重新计算 schema 列表，即使注册表没变
3. 没有缓存机制，10+ 工具时热路径开销明显
