# Skill 系统 + v4 prompt builder 重构 + Slash Command 注册表 — 迭代拆分计划

> 目标：清算 V4 阶段交互层的三件设计债 — 真正的分层 prompt builder、按需加载指令的 progressive disclosure 范式、slash 命令的注册表化 — 让后续 v22-v24 每档新增的命令和上下文构造能干净落地。
>
> 主线节点：V0–V20 是工具系统 / 记忆系统 / 传输层系统三条主线（详见 [CLAUDE.md](../../CLAUDE.md)）。本文档规划的 V21 进入 **skill 子系统 + 交互层**。
>
> 上层路线参见 [docs/system-roadmap.md](../system-roadmap.md) — V21 排在流式（V22）和多 agent（V23）之前的理由也写在那里。

---

## 1. 源项目概述

源项目 [hermes-agent](/Users/qshf/my-project/hermes-agent) 在交互层有三个高度耦合的子系统：

- **Skill 系统** — `tools/skills_tool.py` + `agent/skill_utils.py` + `agent/skill_commands.py` + `skills/<category>/<name>/SKILL.md` 目录树。Markdown YAML frontmatter 描述元信息（`name` / `description` / `platforms` / `metadata`），agent 通过两层加载（tier 1 索引 + tier 2 全文）控制 token 消耗。
- **Slash Command 注册表** — `hermes_cli/commands.py` 用单一 `COMMAND_REGISTRY: list[CommandDef]` 数据驱动所有命令的元信息（name / description / category / aliases / args_hint / subcommands），CLI help / gateway 分发 / Telegram BotCommands / Slack subcommand / autocomplete 全部从同一份注册表派生。
- **Prompt Builder** — `agent/prompt_builder.py` 1456 行，从"5 行 f-string"已经膨胀为骨架 prompt + skill 索引段 + 工具列表段 + 上下文文件扫描 + 注入检测的多段拼装器。

### 1.1 源码规模

| 文件 | 行数 | 职责 |
|------|------|------|
| `tools/skills_tool.py` | 1533 | `skills_list` / `skill_view` 两个 tool 实现 + 元数据校验 + 运行时要求检查 |
| `agent/skill_utils.py` | 511 | `parse_frontmatter` / `skill_matches_platform` / `iter_skill_index_files` / 缓存层 |
| `agent/skill_commands.py` | 501 | `/skill list` / `/skill view` / `/skill reload` slash 处理器 |
| `agent/prompt_builder.py` | 1456 | 骨架 prompt + skill 索引拼装 + 上下文文件扫描 + 注入检测 |
| `hermes_cli/commands.py` | 1721 | `CommandDef` 数据类 + `COMMAND_REGISTRY` + 多端点派生（CLI / gateway / Telegram / Slack） |
| `tools/skill_provenance.py` + `skill_usage.py` + `skills_guard.py` + `skills_hub.py` + `skills_sync.py` | ~5000+ | 来源追溯 / 使用统计 / 安全围栏 / GitHub 同步 — 全部 nano 不复刻 |

总计核心 5722 行，nano 目标裁剪到 800-1100 行。

### 1.2 核心抽象（progressive disclosure 两层加载）

```
启动期                                          运行期（按需）
──────────────────                              ───────────────────────
scan skills/<cat>/<name>/SKILL.md               agent 推理后调 skill_view
        │                                                │
        v                                                v
parse_frontmatter (yaml)                        read SKILL.md 完整 markdown
        │                                                │
        v                                                v
build_skill_index (tier 1)                      返回 tool_call 结果
   只取 name + description                              │
        │                                                v
        v                                       agent 把指令吃进 context
inject 进 system prompt                                 │
   "available skills:                                   v
    - plan: Plan mode...                        按指令展开后续工具调用
    - test-driven-development: ..."
```

agent loop **启动时只看到几百 token 的 skill 索引**（即使 30+ skill 也只占 1-2 KB），完整的几 KB markdown 指令**只在 agent 主动决定要用某个 skill 时**才通过 tier 2 工具拉取。这是上下文经济学（context economics）的标准范式。

### 1.3 Slash Command 注册表的核心抽象

源项目用 `dataclass(frozen=True)` 描述命令：

```python
@dataclass(frozen=True)
class CommandDef:
    name: str                          # "new" / "memory" / "skill"
    description: str                   # 一行人类可读
    category: str                      # "Session" / "Memory" / "Skills"
    aliases: tuple[str, ...] = ()      # ("reset",)
    args_hint: str = ""                # "<prompt>" / "[name]"
    subcommands: tuple[str, ...] = ()  # ("list", "view", "reload")
    cli_only: bool = False
    gateway_only: bool = False
```

所有命令是 `COMMAND_REGISTRY: list[CommandDef]` 一份数据；CLI help / 自动补全 / gateway 分发 / Telegram bot menu 全部 `for cmd in COMMAND_REGISTRY` 派生。**这跟 v0→v1 的"if/elif → 工具注册表"是完全同构的演进**，只是发生在 slash command 这一层而不是 tool 层。

---

## 2. 当前现状（V0–V20 之后的三件设计债）

### 2.1 设计债 #1：v4 prompt builder 名实不符

[agent.py:107-116](../../agent.py#L107-L116) 的 `build_system_prompt` 至今仍是：

```python
def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    provider_tool_names = list(memory_manager.get_all_tool_names())
    all_tool_names = sorted(set(tool_names + provider_tool_names))
    tool_list = "\n".join(f"- `{name}`" for name in all_tool_names)
    memory_block = memory_manager.build_system_prompt()
    return SYSTEM_PROMPT.format(tool_list=tool_list, memory_block=memory_block).strip()
```

10 行 f-string 替换 + 一次 memory_block 拼接。CLAUDE.md 进度表把它写成"system prompt 构建器"，**名实严重不符**。问题不只是"看起来薄"，而是**没有任何分段抽象**：要再加一段（例如 skill 索引、风格约束、安全护栏），唯一的扩展点是改 `SYSTEM_PROMPT` 模板字符串和加新参数，模板里看不出"这一段是干什么的"。

### 2.2 设计债 #2：缺少"按需加载指令"范式

agent 当前所有指令都在 system prompt 一次性灌入。每次 LLM 调用都要把全部规则、风格约束、工作流模板重发一遍。前 20 档已经有：

- v15 的"事后省 token"（上下文压缩）— 删历史
- v20 的"prefix 命中 cache" — 不重发已发过的

但**没有"事前不发"**：哪怕用户这一轮其实只需要"how to write tests"指令，agent 也没法做到"tier 1 看到所有 skill 名 + 描述，tier 2 主动拉详情"。这是 progressive disclosure 范式的关键空缺，与 v15/v20 正交互补。

### 2.3 设计债 #3：slash 命令是 200+ 行 if-elif 链

[agent.py:200-405](../../agent.py#L200-L405) 主循环里 slash 分支占 ~205 行 if-elif 链，每个命令的 usage 文本、subcommand 解析、错误兜底都内联在主循环里：

```
202: if user_input.lower() in ("quit", "exit", "q"):
207: if user_input == "/memory":           # 14 行
224: if user_input.startswith("/load "):   # 13 行
237: if user_input.startswith("/mcp"):     # 56 行（含 list/connect/disconnect/refresh）
293: if user_input.startswith("/plugin"):  # 40 行
333: if user_input == "/tools":            #  5 行
341: if user_input == "/session":          #  3 行
346: if user_input == "/compress":         # 15 行
361: if user_input == "/transport":        # 23 行
384: if user_input == "/new":              #  8 行
394: if user_input.startswith("/resume"):  # 12 行
```

V0→V1 工具系统从"if/elif → 注册表"已经做过一次同样的演进，**slash 这一层却从未做**。后果是：每加一档新版本，主循环就长一段。即将到来的 v22-v24 每档都至少新增 1-3 个 slash 命令：

- v21 自身：`/skill list` / `/skill view <name>` / `/skill reload`
- v22 流式：`/cancel` / `/stream on|off`
- v23 多 agent：`/agents` / `/cancel <id>`
- v24 训练数据：`/insights [--days 30]` / `/trajectory list`

按当前节奏，主循环到 v24 会膨胀到 600+ 行。**先做 slash 注册表，后续每档 slash 改动收敛在 `cli/commands/<name>.py` 一个文件**。

---

## 3. 真实可验证的素材（关键约束）

V21 不需要新增任何外部依赖（pgvector / mock server / DashScope 都不动）：

- **YAML 解析**：复用 Python 标准库不行，需要 `pyyaml`；nano 已隐式依赖（环境里若无可 `pip install pyyaml` 加进 requirements）。源项目用 `CSafeLoader` 优先 + 简单 key:value 兜底，nano 不做兜底（让格式错误显式抛出，教学场景更直接）。
- **示例 skill markdown 内容**：可以直接搬源项目 `skills/software-development/` 下 3-5 个有教学价值的 SKILL.md（`plan` / `test-driven-development` / `systematic-debugging` 都是几十到 200 行，与 nano 工具系统兼容性好），改写头部 frontmatter 适配 nano 的字段集即可。
- **验证场景**：启动 agent 时 banner 应能列出"3 skills loaded"；`/skill list` 显示元数据；user 问"how should I plan this refactor?"时，观察 agent 是否会调 `skill_view("plan")` 而非凭空作答 — 这是 progressive disclosure 真正生效的证据。
- **slash 验证**：`/help` 输出从主循环硬写改为遍历 registry；`agent.py` 主循环 slash 段从 ~205 行 → ~3 行（`cli.dispatch(...)`）。

---

## 4. 版本规划

| 版本 | 标题 | 核心概念 | 真跑验证 | 对应源项目 |
|------|------|---------|---------|-----------|
| **V21** | Skill 系统 + v4 重构 + Slash Command 注册表 | progressive disclosure / 三段式 prompt builder / slash 注册表 + 装饰器 / dispatch | 启动列出 3+ skill；agent 主动调 skill_view；slash 主循环瘦身 | `tools/skills_tool.py` 核心 + `agent/skill_utils.py` + `agent/prompt_builder.py` 子集 + `hermes_cli/commands.py` 子集 |

### 4.1 为什么三件事合并成一档，不拆 v21 / v21.5 / v22

主题统一是"v4 阶段交互层债务清算"，三件事互为前置：

1. **skill 系统需要分层 prompt builder** — tier 1 索引必须作为独立段注入，单靠 `SYSTEM_PROMPT.format()` 没法干净加段。如果先做 skill 后做 prompt builder，第一版 skill 注入会长成 hack。
2. **skill 系统带来 3 个 slash 命令** — `/skill list` / `/skill view` / `/skill reload`，如果 slash 还是 if-elif 链，这 3 个分支会让 agent.py 主循环再涨 60+ 行。如果先做 skill 后做 slash 注册表，做完得回头改 skill 命令的注册方式。
3. **prompt builder 重构是单点改动** — 只动 `build_system_prompt` 函数本身和 `SYSTEM_PROMPT` 常量，没有迁移成本，搭车进 v21 几乎 0 增量复杂度。

强行拆开反而会引入"跨档迁移"的人工成本，违反"每版本看得见的差异最少"原则。

### 4.2 不更细的拆法（被否决）

- "v21 = 只做 slash 注册表 + prompt builder，v22 = skill 系统"
- 否决理由：slash 注册表如果不带 `/skill` 三个命令，整个注册表只是把已有命令搬到注册表 — 重构而无新功能，教学价值不足。skill 命令是"新增功能 + 注册表"的组合，对照源项目最自然。

### 4.3 不更粗的拆法（被否决）

- "v21 = skill + prompt builder + slash + 流式（v22）"
- 否决理由：流式涉及 SSE 解析 / 信号处理 / 取消 token，与 skill 系统是完全独立的两条主题，合并会模糊"这一档解决了什么"。流式独立成 v22 让两档各自的差异有明确边界。

### 4.4 横切关注点（不做）

- **skill 安全围栏 / provenance / 使用统计** — 源项目 `skills_guard.py` / `skill_provenance.py` / `skill_usage.py` ~3000 行，是产品级合规需求；nano 教学场景 1 个用户 + 几个本地示例 skill，不需要。
- **GitHub 同步（Skills Hub）** — 3261 行，依赖外部基础设施。
- **slash 命令 argparse 完整解析** — 源项目用 prompt_toolkit + Completer + AutoSuggest 1100+ 行；nano 用手写分词（`shlex.split` 或 `str.split()` 兜底）即可。
- **slash 跨平台分发**（gateway / Telegram / Slack）— 几千行胶水代码。
- **基于 LLM 的 skill 推荐** — 源项目有"用模型判断当前任务该用哪个 skill"的辅助层，nano 完全靠 agent 自己看 tier 1 索引决定。

---

## 5. V21 详细设计

### 5.1 标题
**Skill 系统 + v4 prompt builder 重构 + Slash Command 注册表 — 一次性还清 v4 阶段三件交互层设计债**

### 5.2 解决的问题
- v4 的 `build_system_prompt` 名实不符，10 行 f-string 没有分段抽象
- agent 缺少 progressive disclosure 范式，所有指令一次性灌入 system prompt
- agent.py 主循环 slash 段 ~205 行 if-elif 链，v22-v24 每档都将持续膨胀

### 5.3 引入的概念

#### 5.3.1 三段式 Prompt Builder（仿源项目 `prompt_builder.py` 的核心思路，去掉上下文文件扫描和注入检测）

新增 `agent/prompt_builder.py`（~120 行）：

```python
class PromptBuilder:
    """三段式系统 prompt 拼装器：骨架 + skill 索引 + 工具列表。"""

    def __init__(self, skill_loader, memory_manager, tool_registry):
        self.skill_loader = skill_loader
        self.memory_manager = memory_manager
        self.tool_registry = tool_registry

    def build(self) -> str:
        sections = [
            self._render_skeleton(),       # 角色 / 风格 / 安全约束
            self._render_skill_index(),    # tier 1 — 仅 name + description
            self._render_memory_block(),   # 复用 memory_manager.build_system_prompt()
            self._render_tool_list(),      # registry + provider 工具名
        ]
        return "\n\n".join(s for s in sections if s).strip()
```

**关键设计**：每段独立可测试、可替换；段间用空行分隔；空段自动跳过（如关闭 skill 时 `_render_skill_index` 返回 ""）。骨架 prompt 移到独立常量 `SKELETON_PROMPT`，与"段间拼装逻辑"解耦。

#### 5.3.2 Skill 加载器与 progressive disclosure（仿源项目 `skill_utils.py` + `skills_tool.py` 核心子集）

新增 `agent/skill_loader.py`（~180 行）：

```python
@dataclass(frozen=True)
class SkillMetadata:
    name: str               # ≤64 chars
    description: str        # ≤1024 chars
    path: Path              # 指向 SKILL.md 绝对路径
    platforms: list[str]    # 空 = all

class SkillLoader:
    """扫描 skills/ 目录，缓存元数据，按需读全文。"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self._cache: dict[str, SkillMetadata] = {}

    def scan(self) -> None:
        """启动期 + /skill reload 时调用。"""
        self._cache.clear()
        for skill_md in self.skills_dir.glob("*/SKILL.md"):
            fm, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            if not skill_matches_platform(fm):
                continue
            meta = SkillMetadata(
                name=fm["name"],
                description=fm["description"],
                path=skill_md,
                platforms=fm.get("platforms", []),
            )
            self._cache[meta.name] = meta

    def list_metadata(self) -> list[SkillMetadata]:
        return sorted(self._cache.values(), key=lambda m: m.name)

    def view(self, name: str) -> str:
        """tier 2 — 返回完整 markdown 内容（含 frontmatter）。"""
        meta = self._cache.get(name)
        if meta is None:
            raise KeyError(f"unknown skill: {name}")
        return meta.path.read_text(encoding="utf-8")
```

**注意几点裁剪**（vs 源项目）：
- 不做条件激活（`requires_toolsets` / `requires_env_vars`）
- 不做嵌套子目录（源项目支持 `skills/<category>/<name>/SKILL.md`，nano 只支持一级 `skills/<name>/SKILL.md`）
- 不做 mtime 失效检测的多级缓存（源项目对启动期 100+ skill 是性能必需，nano 3-5 个不需要）
- 不做 `references/` / `templates/` 子文件（tier 3）— nano 只做 tier 1 + tier 2 两层

#### 5.3.3 `skill_view` 工具（tier 2 入口）

新增 `tools/skill_view_tool.py`（~50 行）：

```python
@tool(
    name="skill_view",
    description="Load full content of a named skill (progressive disclosure tier 2)",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name from the index"},
        },
        "required": ["name"],
    },
)
def skill_view(name: str) -> str:
    try:
        return json.dumps({"output": skill_loader.view(name)})
    except KeyError:
        return json.dumps({"error": f"unknown skill: {name}"})
```

agent 看到 system prompt 中的 skill 索引后，**在它判断需要某个 skill 的详细指令时，主动调这个工具**。tool result 落进 messages → 下一轮推理就有了完整指令。

#### 5.3.4 Slash Command 注册表（仿源项目 `hermes_cli/commands.py` 的核心模式）

新增 `cli/registry.py`（~80 行）：

```python
@dataclass(frozen=True)
class CommandDef:
    name: str                          # "memory" / "skill" / "transport"
    description: str
    handler: Callable[[str, "AgentCtx"], None]   # 接收 args 字符串 + 上下文
    aliases: tuple[str, ...] = ()
    args_hint: str = ""                # for /help

_REGISTRY: dict[str, CommandDef] = {}

def command(name: str, **kwargs):
    """装饰器：@command("/skill", description=..., args_hint="<list|view|reload>")"""
    def decorator(fn):
        canonical = name.lstrip("/")
        cmd = CommandDef(name=canonical, handler=fn, **kwargs)
        _REGISTRY[canonical] = cmd
        for alias in cmd.aliases:
            _REGISTRY[alias] = cmd
        return fn
    return decorator

def dispatch(line: str, ctx: "AgentCtx") -> bool:
    """返回 True 表示已处理（主循环应 continue），False 表示非 slash。"""
    if not line.startswith("/"):
        return False
    parts = line[1:].split(maxsplit=1)
    name = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    cmd = _REGISTRY.get(name)
    if cmd is None:
        print(f"  [error] unknown command: /{name} (try /help)")
        return True
    try:
        cmd.handler(args, ctx)
    except Exception as e:
        print(f"  [error] /{name}: {e}")
    return True
```

**关键设计**：
- 装饰器自动注册，每个命令一个文件 → 文件级隔离 → v22 加 `/cancel` 时只新增 `cli/commands/cancel.py`，主循环零改动
- `dispatch` 返回 bool，主循环只判断"是否被处理"，不知道任何具体命令
- `/help` 自动从 `_REGISTRY` 渲染分组列表（按 description 长度排版）
- alias 通过双键写入实现，无特殊代码路径

#### 5.3.5 `AgentCtx` 上下文对象

新增 `cli/context.py`（~30 行），用 dataclass 把所有 slash handler 需要的运行期对象打包：

```python
@dataclass
class AgentCtx:
    messages: list[dict]          # 当前对话
    chain: TransportChain         # V19 chain
    memory_manager: MemoryManager
    compressor: ContextCompressor
    skill_loader: SkillLoader
    registry: ToolRegistry
    current_session_id: str
    model: str
    # ... 后续按需扩展
```

**关键**：`messages` / `current_session_id` 等可变字段是引用语义，handler 可以直接 mutate（例如 `/new` 把 `ctx.messages[:]` 重新赋值）；不可变字段（如 chain）只读。这避免了"每个 handler 接收 5-8 个位置参数"的冗长签名。

### 5.4 agent.py 改造路径

```python
# 改造前（V20）
def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    provider_tool_names = list(memory_manager.get_all_tool_names())
    all_tool_names = sorted(set(tool_names + provider_tool_names))
    tool_list = "\n".join(f"- `{name}`" for name in all_tool_names)
    memory_block = memory_manager.build_system_prompt()
    return SYSTEM_PROMPT.format(tool_list=tool_list, memory_block=memory_block).strip()

# 主循环 slash 段（~205 行 if-elif）...

# 改造后（V21）
skill_loader = SkillLoader(Path(__file__).parent / "skills")
skill_loader.scan()

prompt_builder = PromptBuilder(
    skill_loader=skill_loader,
    memory_manager=memory_manager,
    tool_registry=registry,
)

ctx = AgentCtx(
    messages=[{"role": "system", "content": prompt_builder.build()}],
    chain=chain, memory_manager=memory_manager,
    compressor=compressor, skill_loader=skill_loader,
    registry=registry, current_session_id=current_session_id,
    model=model,
)

# 主循环 slash 段瘦身后只有 3 行：
while True:
    user_input = input("You > ").strip()
    if cli.dispatch(user_input, ctx):
        continue
    # ... LLM 调用 + tool loop（不变）
```

### 5.5 目录结构（新增）

```
nano_hermes_agent/
├── agent/
│   ├── prompt_builder.py        # 新增：PromptBuilder 三段式
│   └── skill_loader.py          # 新增：SkillLoader + parse_frontmatter
├── cli/
│   ├── __init__.py              # 新增：暴露 dispatch / command 装饰器
│   ├── registry.py              # 新增：CommandDef / _REGISTRY / dispatch
│   ├── context.py               # 新增：AgentCtx
│   └── commands/                # 新增：每个命令一个文件
│       ├── __init__.py          # 自动 import 所有命令触发装饰器注册
│       ├── memory.py            # 抽自 agent.py:207-221
│       ├── plugin.py            # 抽自 agent.py:293-330
│       ├── mcp.py               # 抽自 agent.py:237-290
│       ├── tools.py             # 抽自 agent.py:333-338
│       ├── session.py           # 抽自 agent.py:341-343 + /new + /resume
│       ├── compress.py          # 抽自 agent.py:346-357
│       ├── transport.py         # 抽自 agent.py:361-381
│       ├── load.py              # 抽自 agent.py:224-234
│       ├── skill.py             # 新增：/skill list / view / reload
│       └── help.py              # 新增：从 _REGISTRY 渲染
├── tools/
│   └── skill_view_tool.py       # 新增：tier 2 入口
└── skills/                      # 新增：3-5 个示例
    ├── plan/SKILL.md
    ├── test-driven-development/SKILL.md
    └── systematic-debugging/SKILL.md
```

### 5.6 对应源项目

- `agent/skill_utils.py:52-86` `parse_frontmatter` — nano 同名函数 ~25 行（去掉 fallback 解析、CSafeLoader 优化）
- `agent/skill_utils.py:92-115` `skill_matches_platform` — nano 同名函数 ~15 行（去掉 ENV_VAR_NAME 检查）
- `tools/skills_tool.py:674-848` `skills_list` — nano 用 `SkillLoader.list_metadata()` + 在 prompt builder 渲染替代
- `tools/skills_tool.py:849-1000` `skill_view` — nano `tools/skill_view_tool.py` ~50 行
- `agent/prompt_builder.py:1-200` 骨架 + skill 段 — nano `agent/prompt_builder.py` ~120 行
- `hermes_cli/commands.py:46-58` `CommandDef` dataclass — nano `cli/registry.py` ~30 行（去掉 gateway / Telegram / Slack 字段）
- `hermes_cli/commands.py:64-220` `COMMAND_REGISTRY` 数据 — nano 改用装饰器注册而非中央数据表（更适合 200-300 行规模）

### 5.7 预估代码量

| 文件 | 行数 | 说明 |
|------|------|------|
| `agent/prompt_builder.py` | ~120 | PromptBuilder 三段式 + SKELETON_PROMPT 常量 |
| `agent/skill_loader.py` | ~180 | SkillLoader + SkillMetadata + parse_frontmatter + skill_matches_platform |
| `tools/skill_view_tool.py` | ~50 | tier 2 工具实现 |
| `cli/registry.py` | ~80 | CommandDef + 装饰器 + dispatch |
| `cli/context.py` | ~30 | AgentCtx dataclass |
| `cli/commands/__init__.py` | ~20 | 自动 import 触发装饰器 |
| `cli/commands/<10 个命令文件>` | ~250 | 每个 15-30 行（从 agent.py 抽出 + 装饰器化） |
| `skills/<3-5 个示例>/SKILL.md` | — | 改写自源项目 plan / TDD / systematic-debugging |
| **新增** | **~730** | — |
| `agent.py` | -210/+30 | 删 slash if-elif 链 + 删 build_system_prompt + 接入 PromptBuilder + ctx 构造 + dispatch 调用 |

合计 800-1100 行核心代码（含示例 skill markdown）。

### 5.8 暴露的下一档问题

- skill 索引段固定挂在 system prompt 头部 — 与 v20 prompt cache 的 `system_and_3` 策略协同良好（system 段稳定 = 高命中率），但 **`/skill reload` 后 cache 必失配**。需要在 reload 时主动 reset chain 的 cache 统计 / 提示用户。
- agent 主动调 `skill_view` 的"决策质量"完全靠 LLM 推理 — 没有"按工具名匹配自动建议"的辅助层。教学场景可接受；产品级会暴露"agent 不知道有哪个 skill 适用"的失误。
- slash 注册表目前用装饰器自动注册，但**没有强制启动期 import 触发** — 如果 `cli/commands/<x>.py` 没被 import，命令不会出现。`cli/commands/__init__.py` 的"自动 import"是约定而非强制；漏注册的命令会静默缺失。
- skill markdown 中的指令可能与 system prompt 骨架冲突（例如 skill 说"do X"骨架说"never X"）— 没有冲突检测层。
- `parse_frontmatter` 用 yaml 解析失败时直接抛出 — 教学场景方便排错，产品场景需要 fallback。

→ 后续可拆 V21.1（reload 协调 cache）/ V21.2（启动期强制扫描注册）。

### 5.9 验证方式

#### 5.9.1 不变量脚本 `scripts/test_v21_skill.py`（目标 12-15 项）

**SkillLoader 行为**（5 项）：
- 扫描空目录 → `list_metadata()` 返回空
- 扫描 3 个 skill → 返回排序后的 3 条元数据
- 缺失 frontmatter 字段 (name/description) → 抛 KeyError 而非静默
- platforms 不匹配当前 OS → 该 skill 不出现在索引
- `/skill reload` 后能感知新增 skill 文件

**PromptBuilder 行为**（4 项）：
- 关 skill（loader 为 None 或 list 为空）→ 输出无 skill 段
- 开 skill（3 条）→ 输出含 "## available skills" + 3 行
- 各段顺序固定：骨架 → skill → memory → tools
- 段间空行严格一个

**Slash dispatch 行为**（4 项）：
- 已注册命令路由到 handler
- 未注册命令打印 error 但不崩
- alias 等价于主名
- handler 异常被 catch + 打印 error，主循环继续

#### 5.9.2 真跑场景

- 启动 banner 应打印 `Skills: 3 loaded (plan, test-driven-development, systematic-debugging)`
- 输入 `/help` → 输出按 description 列出的所有命令（自动从 registry 渲染）
- 输入 `/skill list` → 输出 3 条带 description 的元数据
- 输入"我想为这次改动做个详细计划"→ 观察 agent 是否调 `skill_view("plan")` 而非凭空作答
- 输入 `/skill view plan` → 直接读 markdown 全文（slash 命令旁路 LLM）
- 输入 `/skill reload` 后修改 `skills/plan/SKILL.md` 再调 `/skill view plan` → 看到新内容
- `agent.py` 行数从 538 → ~370（slash 段 ~205 行 + build_system_prompt 10 行 → 约 30 行）

#### 5.9.3 回归

- V12/V13/V14/V16/V17/V18/V19/V20 现有脚本全过（agent loop 行为零变化，仅交互层重构）
- 关 skill（env `SKILLS_ENABLED=0`）后退化为类 v4 行为，所有命令仍工作

---

## 6. V21 之后的衔接（路线图引用）

V21 完成后路线图（详见 [docs/system-roadmap.md](../system-roadmap.md)）：

```
v21  Skill 系统 + v4 重构 + Slash Command 重构  ← 本文档（上下文经济学 / 三件 v4 设计债一次清）
        ↓
v22  流式输出 + 中断                          ← Transport 弧线收尾 / 多 agent 前置
        ↓
v23  多智能体 delegate                        ← 并发 + 隔离 + 抽象体检（子 agent 复用 v21 skill 索引）
        ↓
v24  Trajectory + Insights                   ← 数据飞轮起点
```

**V21 对后续档的具体复利**：

- v22 加 `/cancel` / `/stream` 命令 → 直接新增 `cli/commands/cancel.py` + `stream.py`，主循环零改动
- v23 子 agent 拿到精简 skill 索引（只筛相关分类）→ `SkillLoader.list_metadata(category=...)` 即可
- v23 加 `/agents` / `/cancel <id>` 命令 → 同 v22
- v24 加 `/insights` / `/trajectory` 命令 → 同 v22
- v23 子 agent 的 system prompt 通过 `PromptBuilder` 重建（仅注入受限 skill 索引 + 受限工具列表 + 子 goal）→ 不必复制粘贴 system prompt 模板

---

## 7. 决策一句话总结

| 决策 | 选项 | 没选 | 原因 |
|------|------|------|------|
| 三件事是否合一档 | 合一档 (v21) | 拆 v21/v21.5/v22 | 三件互为前置，拆开会引入跨档迁移成本 |
| Prompt builder 形态 | 三段式 class 拼装 | 继续 f-string format | 段独立可测试 / 可扩展 / 空段自动跳过 |
| Skill 加载层级 | tier 1 + tier 2 | tier 1+2+3（references/templates） | nano 教学场景 3-5 个 skill 不需要嵌套引用 |
| Skill 嵌套结构 | `skills/<name>/SKILL.md` 一级 | 源项目 `skills/<cat>/<name>/` 两级 | 类别字段保留在 frontmatter `metadata.category`，文件结构扁平 |
| Slash 注册方式 | 装饰器自动注册 | 中央 list 数据驱动（源项目） | nano 命令 ≤ 20 个，装饰器 + 文件级隔离更轻 |
| Slash handler 入参 | `(args: str, ctx: AgentCtx)` | 多个位置参数 | 单一上下文对象避免 5-8 参冗长签名 |
| YAML 解析失败行为 | 直接抛出 | 简单 key:value 兜底（源项目） | 教学场景显式失败更易排错 |
| 取消 GitHub 同步 / Hub | 不复刻 | 复刻 | 3261 行外部基础设施，与 AI 概念无关 |
| 取消 skills_guard / provenance / usage | 不复刻 | 复刻 | ~3000 行产品级合规需求，nano 单用户场景不需要 |
| 取消 prompt_toolkit 自动补全 | 不复刻 | 复刻 | 1100+ 行 UI 胶水，手写分词足够 |

完整的"为什么这样选 / 没那样选 / 真实踩坑 / 验证方式"在落地后写入 `docs/decisions/v21.md`。


