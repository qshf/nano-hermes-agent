# v26.1 Skill 可用性门控 —— 知识点

## 一、背景

v26.0 把 skill 从单文件升级为目录包，让 skill 能携带资源。但它漏了一件事：**skill 没法说自己用不用得了**。

比如一个"朗读进度"的 skill 依赖 TTS API key，key 没配时 agent 照样在 system prompt 里看到它、照样可以调 `skill_view` 展开说明书，然后才发现"哦，我没法用"。这几轮工具调用的 token 白花了。

v26.1 补上这块：skill 现在可以在 frontmatter 里声明依赖，系统根据依赖是否满足来决定「显示」还是「标记」。

---

## 二、数据结构变更

`SkillMetadata` 新增三个字段，加上两个便利方法：

> 📂 [`agent/skill_loader.py:51-86`](../../../agent/skill_loader.py#L51)

```python
@dataclass(frozen=True)
class SkillMetadata:
    """tier 1 元信息 — 注入 system prompt 的最小集。

    V26.1：新增可用性门控字段（仿源项目 ``skill_utils.py`` 的
    ``required_environment_variables`` + ``metadata.requires_tools/toolsets``
    子集，去 HERMES 前缀）。两种门控策略不同：

    - ``required_env_vars`` 缺失 → **软标记**（仍进索引，渲染层标 ⚠ setup_needed），
      因为 agent 需要「引导用户配置」的交互机会；
    - ``requires_tools`` / ``requires_toolsets`` 不满足 → **硬隐藏**（不进索引），
      因为工具不存在时 skill 根本无法工作，留着只浪费 token。
    """

    name: str
    description: str
    path: Path
    platforms: tuple[str, ...]
    skill_dir: Path = Path(".")
    required_env_vars: tuple[str, ...] = ()     # V26.1
    requires_tools: tuple[str, ...] = ()         # V26.1
    requires_toolsets: tuple[str, ...] = ()      # V26.1

    def missing_env_vars(self) -> list[str]:
        """声明的 env 中当前未设置（或为空串）的那些。"""
        return [v for v in self.required_env_vars if not os.environ.get(v)]

    @property
    def setup_needed(self) -> bool:
        """有任一 required env 缺失 → 需要用户配置才能用。"""
        return bool(self.missing_env_vars())
```

### frontmatter 写法

skill 作者在 SKILL.md 的 YAML 头部声明依赖：

```yaml
---
name: voice-runtime
description: "Speak progress updates aloud via a TTS backend."
required_environment_variables:        # 顶层：需要哪些 env
  - DASHSCOPE_API_KEY
metadata:
  requires_tools:                      # metadata 下：需要哪些工具
    - terminal
---
```

两种 env 声明写法都支持（与源项目兼容）：

```yaml
# 写法 A：纯名字列表
required_environment_variables:
  - DASHSCOPE_API_KEY
  - OTHER_KEY

# 写法 B：带 help 的对象列表（nano 只取 name，忽略 help）
required_environment_variables:
  - name: DASHSCOPE_API_KEY
    help: "从阿里云 DashScope 控制台获取"
```

---

## 三、两种门控策略：为什么不同

这是 v26.1 最核心的判断：**门控不是一刀切的"显示/不显示"，而是按「缺失能不能被用户补救」分流。**

| 门控 | 策略 | 行为 | 为什么 |
|------|------|------|--------|
| `required_env_vars` 缺失 | **软标记** | 仍进索引，追加 `⚠ (setup: set X)` | 用户改 `.env` 就能补救——agent 可以引导用户去做，硬隐藏会让这个交互无从发生 |
| `requires_tools/toolsets` 不满足 | **硬隐藏** | 不进 system prompt 索引 | 工具不存在时代码层面无法补救——留着只浪费 token |

> **"硬隐藏"不等于"卸载"**：skill 仍在 `SkillLoader` 缓存中，`loader.get(name)` 能拿到，`skill_view(name)` 仍可直接调用。只是 agent 在 system prompt 里看不到它，不会主动去调。

### 具体流程

```
skill 声明了 requires_tools: [terminal]

场景 A：当前 agent 有 terminal 工具
  → 进 system prompt 索引 ✅

场景 B：当前 agent 没有 terminal 工具
  → 不进 system prompt 索引（硬隐藏）
  → agent 不会主动知道它，但 skill_view("voice-runtime") 仍能正常返回
  → /skill list 不受影响（走无门控路径，全显示，方便人工排查）
```

```
skill 声明了 required_environment_variables: [DASHSCOPE_API_KEY]

场景 A：env 已设置
  → 进索引 ✅，无标记

场景 B：env 未设置
  → 进索引 ✅，但带 ⚠ (setup: set DASHSCOPE_API_KEY)
  → agent 看到后可以引导用户："你需要设置 DASHSCOPE_API_KEY 才能用 voice-runtime"
```

---

## 四、`list_metadata` 的门控实现

> 📂 [`agent/skill_loader.py:261-289`](../../../agent/skill_loader.py#L261)

```python
def list_metadata(
    self,
    available_tools: list[str] | None = None,
    available_toolsets: list[str] | None = None,
) -> list[SkillMetadata]:
    """返回按 name 排序的 metadata 列表（V26.1 加可用性门控）。

    - ``requires_tools`` / ``requires_toolsets`` 任一不满足 → **硬隐藏**
    - ``required_env_vars`` 缺失 → **不在这里过滤**：交给渲染层软标记

    向后兼容：``available_tools=None`` 表示「调用方没传工具信息」
    → 不做硬隐藏，全显示（V21.x / 单测路径行为不变）。
    """
    out: list[SkillMetadata] = []
    at = set(available_tools) if available_tools is not None else None
    ats = set(available_toolsets) if available_toolsets is not None else None
    for m in sorted(self._cache.values(), key=lambda x: x.name):
        if at is not None and any(t not in at for t in m.requires_tools):
            continue  # 硬隐藏：缺工具
        if ats is not None and any(s not in ats for s in m.requires_toolsets):
            continue  # 硬隐藏：缺 toolset
        out.append(m)  # env 缺失不在此过滤 —— 交给渲染层标记
    return out
```

### `None` vs `[]` 的语义

```python
# None = "不知道有哪些工具"（向后兼容，全显示）
loader.list_metadata()                    # 全显示

# [] = "确实没有任何工具"（所有声明了 requires_tools 的 skill 全隐藏）
loader.list_metadata(available_tools=[])  # 硬隐藏所有有工具依赖的 skill
```

---

## 五、`prompt_builder` 如何渲染

只有 `PromptBuilder` 显式传入可用工具时才触发硬隐藏。`/skill list` 路径不传 → 全显示，方便人工排查。

> 📂 [`agent/prompt_builder.py:158-178`](../../../agent/prompt_builder.py#L158)

```python
def _render_skill_index(self) -> str:
    """V21.3 tier 1 skill 索引（仅 name + description）。

    V26.1 可用性门控：把当前可用工具/toolset 传给 ``list_metadata``，
    ``requires_tools``/``requires_toolsets`` 不满足的 skill **硬隐藏**；
    ``required_env_vars`` 缺失的 skill 仍进索引但追加 ``⚠ (setup: set X)``
    **软标记**。
    """
    metadata = list(
        self._skill_loader.list_metadata(
            available_tools=self._available_tool_names(),
            available_toolsets=list(self._enabled_toolsets),
        )
    )
    lines = ["## available skills"]
    for meta in metadata:
        suffix = ""
        missing = getattr(meta, "missing_env_vars", lambda: [])()
        if missing:
            suffix = "  ⚠ (setup: set " + ", ".join(missing) + ")"
        lines.append(f"- {meta.name}: {meta.description}{suffix}")
    return "\n".join(lines)
```

### agent 实际在 system prompt 里看到的

```
## available skills
- code-reading: 系统性阅读源码：定位入口、追踪数据流、识别模式、验证理解
- novel-writing: 小说撰写：用户提供大纲和画面，AI据此撰写完整剧情段落
- plan: Plan mode: write a markdown plan instead of executing
- systematic-debugging: 4-phase root-cause debugging
- test-driven-development: TDD: RED-GREEN-REFACTOR
- voice-runtime: Speak progress updates aloud via a TTS backend  ⚠ (setup: set DASHSCOPE_API_KEY)
```

`voice-runtime` env 未配时带 ⚠ 标记，其他 skill 无标记。`echo-formatter` 因为声明了 `requires_tools: [nonexistent-tool]`（不存在的工具），被硬隐藏，不出现。

---

## 六、`skill_view` 工具回填

> 📂 [`tools/skill_view_tool.py:127-134`](../../../tools/skill_view_tool.py#L127)

```python
# V26.1 可用性回填 —— 让 agent 在读完 SKILL.md 后立刻知道这个 skill 能不能用
meta = _skill_loader.get(name) if hasattr(_skill_loader, "get") else None
missing = list(meta.missing_env_vars()) if meta is not None else []
payload["readiness_status"] = "setup_needed" if missing else "available"
if missing:
    payload["missing_env_vars"] = missing
    payload["setup_needed"] = True
```

agent 调用 `skill_view("voice-runtime")` 后收到（假设 `terminal` 可用、`DASHSCOPE_API_KEY` 未设）：

```json
{
  "output": "# voice-runtime\n\n把 agent 的进度更新念出来的演示 skill...",
  "readiness_status": "setup_needed",
  "missing_env_vars": ["DASHSCOPE_API_KEY"],
  "setup_needed": true
}
```

---

## 七、关键设计决策

| 决策 | 选项 | 没选 | 原因 |
|------|------|------|------|
| env 缺失策略 | 软标记（进索引标 ⚠） | 硬隐藏 | agent 需要「引导用户配置」的交互机会 |
| requires_tools 策略 | 硬隐藏 | 软标记 | 工具不存在 skill 无法工作，留着浪费 token |
| 兼容默认 | `available_tools=None`→全显示 | 默认空 set→全隐藏 | 「无信息」与「确实没工具」语义不同 |
| `available_tools` 来源 | 复用 builder 现有依赖 | 加新构造参数 | 可用工具是已知派生信息，非新输入 |
| `fallback_for_*` | 不做 | 移植 | nano 无「兜底 skill」概念（YAGNI） |
| secret 配置 | 只检查 + 标记 | 交互式 capture | 见下方「什么是交互式 capture」——这是第九节冗余矛盾的根因 |

### 什么是「交互式 capture」，为什么不做

源项目在 skill **加载那一刻**发现缺 env 时，不是只标记，而是**当场向用户要这个 key**：

```
skill 加载 → 发现缺 required_environment_variables
  → _capture_required_environment_variables（skills_tool.py:295）
  → _secret_capture_callback → prompt_for_secret（hermes_cli/callbacks.py:66）
  → getpass / TUI 弹隐藏输入框，用户当场输入
  → save_env_value_secure 写进 ~/.hermes/.env（key 绝不喂给模型）
  → capture 之后「仍然」缺的，才标 setup_needed
```

所以源项目的 `setup_needed` 是「我已经主动问过、用户跳过或填错」之后的**残留状态**，不是「启动时一查就有」的静态标记。

nano 砍掉了「弹框 + 写盘」这一步（要绑 TUI 事件循环、隐藏输入、安全落盘、120s 超时 + queue，对教学版是过度工程——YAGNI），只保留「读环境变量、缺了就标记给 agent 看」。

**这正是第九节冗余矛盾的根因**：因为 nano 不能在加载时当场把 key 要过来，它只能提前在 tier 1 用 ⚠ 预警「这个 skill 缺配置」；而源项目能当场解决，所以 tier 1 根本不放 env 标记，也就不存在这份冗余。

---

## 八、完整示例：`voice-runtime` + `echo-formatter`

两个 demo skill，用于观察门控效果。

### voice-runtime

```
skills/voice-runtime/
└── SKILL.md
```

声明了 `DASHSCOPE_API_KEY` + `requires_tools: [terminal]`。

运行时的完整信息链：

```
tier 1 — system prompt 索引（terminal 可用、API key 未设）：
  "voice-runtime: Speak progress updates aloud ...  ⚠ (setup: set DASHSCOPE_API_KEY)"

  如果 terminal 不可用：
  → 直接不出现（硬隐藏）

tier 2 — agent 调用 skill_view("voice-runtime")：
  → output: voice-runtime 说明书全文
  → readiness_status: "setup_needed"
  → missing_env_vars: ["DASHSCOPE_API_KEY"]
  → setup_needed: true
```

### echo-formatter

```
skills/echo-formatter/
└── SKILL.md
```

声明了 `ECHO_FORMATTER_TOKEN`（未设置）+ `requires_tools: [nonexistent-tool]`（不存在的工具）。

运行时的完整信息链：

```
tier 1 — system prompt 索引（PromptBuilder 传入 available_tools）：
  → echo-formatter 不出现（硬隐藏：nonexistent-tool 不在可用工具列表中）

  但 loader 缓存中仍在：
  → loader.get("echo-formatter") 正常返回 metadata
  → skill_view("echo-formatter") 仍可调用

/ skill list（无门控参数）：
  → echo-formatter 出现并标 ⚠ (setup: set ECHO_FORMATTER_TOKEN)
  → 因为 /skill list 不传 available_tools，不做硬隐藏，方便人工排查
```

> 📂 实物见 [`skills/voice-runtime/`](../../../skills/voice-runtime/) 和 [`skills/echo-formatter/`](../../../skills/echo-formatter/)

---

## 九、⚠ 矛盾点：`skill_view` 回填与 tier 1 软标记的信息重复

### 两个通道，同一事实

v26.1 在两条独立的通道里传递了**完全相同的信息**——"这个 skill 缺哪些 env"（以 `voice-runtime` 为例，假设 `terminal` 工具可用、`DASHSCOPE_API_KEY` 未设）：

| 通道 | 位置 | 格式 | agent 何时看到 |
|------|------|------|----------------|
| A | system prompt tier 1 索引 | `⚠ (setup: set DASHSCOPE_API_KEY)` | 对话第一轮 |
| B | `skill_view` tier 2 tool result | `"readiness_status": "setup_needed", "missing_env_vars": ["DASHSCOPE_API_KEY"]` | 调 `skill_view` 后 |

### agent 的时间线

按 agent 实际对话顺序：

```
第 1 轮 — system prompt 注入：
  agent 看到 → "voice-runtime: Speak progress updates ...  ⚠ (setup: set DASHSCOPE_API_KEY)"
  agent 此时已经知道：voice-runtime 不能用，缺 DASHSCOPE_API_KEY

第 N 轮 — agent 决定深入了解 voice-runtime，调 skill_view("voice-runtime")：
  tool result → {..., "readiness_status": "setup_needed", ...}
  信息增量：零。agent 在第 1 轮就已经知道了。
```

### 为什么这是问题

通道 B 存在的理由（决策文档里的原话）：

> "让 agent 读完说明书立刻知道「这个 skill 现在能不能用、缺什么」，而不必再调一次才发现。"

但 agent **在调 `skill_view` 之前就已经知道**了。它不是因为读了说明书才知道的——它是因为 system prompt 里的 `⚠` 标记才知道的。

换句话说：如果 agent 没看到 `⚠` 标记就不会调 `skill_view`（它没理由去深入了解一个"看起来正常"的 skill），而一旦它调了，说明它已经看到了 `⚠` 标记。

**回填的 `readiness_status` / `missing_env_vars` / `setup_needed` 只是把 tier 1 已经传递过的事实换了一种格式再传了一遍。信息增量为零。**

### 对比：什么时候回填不冗余

回填有价值的前提是：tier 1 没有传递这个信息。例如 v26.0 的 `linked_files`：

```
tier 1 → "code-reading: 系统性阅读源码..."
         （不知道这个 skill 还带了什么资源）

tier 2 → "linked_files: {references: [tracing-guide.md]}"
         （新增信息：原来它还有个 tracing guide！）
```

这里 tier 2 传递的是 tier 1 没有的信息——agent 的认知被拓展了。回填有价值。

但 v26.1 的 `readiness_status` 不同：tier 1 已经通过 `⚠` 标记传递了同样的信息。回填没有拓展 agent 的认知——它只是重复。

### 源项目对照：这份冗余是 nano 自己引入的

去源项目 hermes-agent 对照同样两条路径，会发现**这个矛盾在源项目里根本不存在**——它做了一刀干净的分层切割：

| 维度 | 进 tier 1 索引？ | 在 tier 2 出现？ |
|------|----------------|----------------|
| `requires_tools/toolsets`（代码层不可补救） | 硬隐藏，只决定显示与否 | — |
| `required_env_vars`（用户可补救） | **从不出现**（无 ⚠ 标记） | 加载时第一次出现，是真·新信息 |

- tier 1 的 [`build_skills_system_prompt`](../../../../hermes-agent/agent/prompt_builder.py) 门控只走 [`_skill_should_show`](../../../../hermes-agent/agent/prompt_builder.py)，**只看 tools/toolsets，完全不碰 env**；索引行只输出 `- {name}: {desc}`，没有任何 setup 标记。
- env readiness（`required_environment_variables` / `missing_*` / `setup_needed` / `readiness_status`）**只在 tier 2 加载 skill 的 tool result 里第一次出现**（[`skills_tool.py:1360`](../../../../hermes-agent/tools/skills_tool.py)）。

所以源项目里 agent 在 tier 2 看到 `setup_needed` 时，这**确实是它之前不知道的**——和 v26.0 的 `linked_files` 性质一样，是认知拓展，不是重复。

**nano 之所以冗余，是因为它移植时多做了一步源项目没做的事**：把 env 缺失也提升到了 tier 1 软标记。而它必须这么做，是因为它砍掉了源项目的「交互式 capture」（见第七节）——不能在加载时当场要 key，就只能提前在 tier 1 预警。**冗余是放弃交互式 capture 的连带代价，是一个有意识的教学权衡，不是 bug。**

### 两条出路

1. **删 tier 1 的 ⚠ 软标记**（完全对齐源项目）：tier 1 回归纯 `name: description`，readiness 只在 `skill_view` 回填，回填重新变成真·新信息。代价：agent 第一轮看不到「哪些 skill 需要配置」，少了主动引导用户的契机——而这正是 nano 当初加 ⚠ 的理由。
2. **保留 ⚠ + 接受 tier 2 回填**：理由是两个通道消费形态不同——tier 1 是被动扫一眼的自然语言，tier 2 是 agent 已决定深入、需要一个结构化、机器可判定的 `setup_needed` 布尔来决定下一步（引导 vs 执行），而不必去正则解析 system prompt 里的 ⚠ 文本。「同事实、不同消费形态」在工程上常见，不算纯浪费。

源项目走的是接近路线 1 的思路，但它有 nano 没有的交互式 capture 兜底；nano 在放弃 capture 的前提下选了路线 2 的雏形。

---

## 附录：涉及文件一览

| 文件 | 涉及内容 |
|------|----------|
| [`agent/skill_loader.py`](../../../agent/skill_loader.py) | `SkillMetadata` 新增三字段 + `missing_env_vars()` / `setup_needed`；`_parse_env_vars` / `_parse_str_list`；`list_metadata` 门控；`get()` 访问器 |
| [`agent/prompt_builder.py`](../../../agent/prompt_builder.py) | `_render_skill_index` 传 `available_tools`（硬隐藏）+ ⚠ 软标记；新增 `_available_tool_names()` |
| [`tools/skill_view_tool.py`](../../../tools/skill_view_tool.py) | tier 2 结果回填 `readiness_status` / `missing_env_vars` / `setup_needed` |
| [`cli/commands/skill.py`](../../../cli/commands/skill.py) | `/skill list` 给 env 缺失的 skill 标 ⚠ setup |
| [`skills/voice-runtime/`](../../../skills/voice-runtime/) | demo skill：声明 `DASHSCOPE_API_KEY` + `requires_tools: [terminal]` |
| [`skills/echo-formatter/`](../../../skills/echo-formatter/) | debug skill：声明 `ECHO_FORMATTER_TOKEN` + `requires_tools: [nonexistent-tool]`（观察硬隐藏） |
| [`docs/decisions/v26.1.md`](../../../docs/decisions/v26.1.md) | 完整决策记录 |
