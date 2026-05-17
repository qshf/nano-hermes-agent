# Nano Hermes Agent

从零搭建 AI Agent 工具系统的教程仓库。每个 git 分支对应一个迭代版本。

## 分支说明

| 分支 | 版本 | 核心变化 |
|------|------|----------|
| `v0` | 最小可用 | 无注册表，if/elif 分发 |
| `v1` | 注册表 | 自注册 + dispatch（计划中） |
| `v2` | Toolsets | 名字展开 + check_fn（计划中） |
| `v3` | 缓存 | generation + TTL（计划中） |
| `v4` | 类型修复 | coerce + 异步桥接（计划中） |
| `v5` | 插件钩子 | pre/post/transform（计划中） |

## 快速开始

```bash
# 克隆并切到 v0 分支
git clone <repo-url>
git checkout v0

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

## 当前版本：V0 — 最小可用

用最少的代码让 LLM 能调用工具并拿到结果。

```
nano_hermes_agent/
├── agent.py              # 主循环：对话 + LLM 调用 + 字典分发
├── tools/
│   ├── terminal_tool.py  # schema + handler（执行 shell 命令）
│   └── read_file_tool.py # schema + handler（读文件带行号）
├── pyproject.toml        # uv 项目配置
├── .env.example          # 环境变量模板
└── .gitignore
```

### V0 的问题（V1 要解决的）

1. 新增工具要改 agent.py 三处：import schema、import handler、加到 DISPATCH 字典
2. schema 和 handler 的绑定关系是隐式的，靠人记住名字要对应
3. 10 个工具时 agent.py 就变成一坨 import
4. 没有"工具当前能不能用"的运行时判断
