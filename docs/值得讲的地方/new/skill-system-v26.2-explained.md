# v26.2 Skill 安全 Token 替换 —— 知识点

## 一、背景

v26.0 让 skill 能携带资源（tier 3），v26.1 让 skill 能声明依赖（门控）。还剩最后一个问题没解决。

假设你给 `plan` skill 写了一段指令：

```markdown
## Before you finish

Run through the review checklist. Read it with:

skill_view("plan", "references/checklist.md")

The checklist lives at /Users/qshf/.../skills/plan/references/checklist.md on disk.
```

"skill 作者不该在自己的 SKILL.md 里写死绝对路径"——这个道理谁都懂。但总得有一个方式让 agent 知道"checklist 在哪"。如果 skill 本身就是一个自包含的**目录包**，路径就是 `skill_dir/references/checklist.md`——问题是 skill 不知道自己的目录叫什么。

源项目用 `${HERMES_SKILL_DIR}` token 解决：skill 作者写 token，运行时系统替成真实路径。v26.2 把这个机制移植过来，但做了一道关键的减法——**只做安全的字符串替换，绝不碰命令执行**。

---

## 二、核心机制：`substitute_tokens()`

> 📂 [`agent/skill_preprocessing.py`](../../../agent/skill_preprocessing.py)（77 行）

```python
import re
from pathlib import Path

# 固定白名单：只认这两个 token。其余 ${...} 一律不动。
_TOKEN_RE = re.compile(r"\$\{(SKILL_DIR|SESSION_ID)\}")


def substitute_tokens(
    content: str,
    skill_dir: Path | None,
    session_id: str | None,
) -> str:
    """把 SKILL.md 正文里的 ${SKILL_DIR} / ${SESSION_ID} 替换为具体值。

    只替换有具体值的 token：skill_dir 为 None 时 ${SKILL_DIR} 原样保留，
    session_id 为 None/空时 ${SESSION_ID} 原样保留——让作者排错。
    白名单之外的 token（如 ${OPENAI_API_KEY} / ${FOO}）不在正则捕获范围内，
    天然原样保留——密钥永远不会被替进 context。
    """
    if not content:
        return content

    skill_dir_str = str(skill_dir) if skill_dir else None

    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "SKILL_DIR" and skill_dir_str:
            return skill_dir_str
        if token == "SESSION_ID" and session_id:
            return str(session_id)
        return match.group(0)  # 无值 → 原样保留

    return _TOKEN_RE.sub(_replace, content)
```

### 两个 token

| Token | 替换为 | 无值时 |
|-------|--------|--------|
| `${SKILL_DIR}` | skill 目录的绝对路径，如 `/Users/.../skills/plan` | 原样保留（理论上不会发生，skill_dir 始终有值） |
| `${SESSION_ID}` | 当前会话 ID，如 `abc123-def456` | **原样保留**——让作者一眼看出"没拿到值"，而非静默替成空串 |

### 设计要点

- **有值才替，无值原样保留**：`session_id=None` 时 `${SESSION_ID}` 不动。这让 skill 作者能一眼发现"运行期没拿到那个值"，而不是事后追查一个空字符串从哪来。
- **延迟 import**：`substitute_tokens` 在 `SkillLoader.view()` 内部延迟 import，未配 skill 时不付加载代价，也避免 `agent/` 子包内的环形依赖。

---

## 三、为什么是白名单而不是开放任意 env 名

一个直觉是"把 `${ANY_ENV_NAME}` 都替成环境变量的值"——灵活、简单。但这是安全灾难。

```markdown
# 如果开放任意 env 替换，skill 作者可以写：
API key 是 ${OPENAI_API_KEY}

# agent 调用 skill_view 后看到：
API key 是 sk-abc123def456...

# 这把密钥直接写进了 LLM context。
```

v26.2 用**固定白名单正则** `\$\{(SKILL_DIR|SESSION_ID)\}`——不在这两个名字之内的 `${...}` **根本不匹配正则**，天然原样保留。不存在"检查→拒绝"的步骤，因为 `${OPENAI_API_KEY}` 从一开始就不会被 `_TOKEN_RE` 捕获。

这 4 字节正则就是整条安全防线的全部代码。

---

## 四、⚠ 安全红线：绝不移植内联 shell

这是 v26.2 最关键的减法——源项目 `skill_preprocessing.py` 有两个机制：

| 机制 | 语法 | 行为 | 风险 |
|------|------|------|------|
| 变量替换 | `${SKILL_DIR}` | 纯字符串替换 | 安全 |
| 内联 shell | `` !`cmd` `` | `subprocess.run(["bash", "-c", cmd])` | **任意命令执行** |

源项目默认 `inline_shell=False` 关闭，但函数存在本身就是一个攻击面：假如某次配置错误或重构不小心开了，skill 文件就会变成任意代码执行的载体。

nano 教学版的决策是**连函数都不写进来**。`agent/skill_preprocessing.py` 整个文件没有一行 `subprocess`，没有 `bash`，没有 `shell`。不是"默认关闭"，是"不存在"。

> 这是 skill 系统补全计划 §8 安全护栏的硬约束，不是可配置项。

---

## 五、两个接入点

### 5.1 `SkillLoader.view()` — 返回前替换

> 📂 [`agent/skill_loader.py:293-316`](../../../agent/skill_loader.py#L293)

```python
def view(self, name: str, session_id: str | None = None) -> str:
    """V26.2：返回前过一遍安全 token 替换。

    session_id=None 时 ${SESSION_ID} 原样保留（向后兼容：V21.x
    旧调用方不传 session_id，行为是「只替 SKILL_DIR」）。
    """
    meta = self._cache.get(name)
    if meta is None:
        raise KeyError(f"unknown skill: {name}")
    content = meta.path.read_text(encoding="utf-8")

    from agent.skill_preprocessing import substitute_tokens
    return substitute_tokens(content, meta.skill_dir, session_id)
```

### 5.2 `skill_view` 工具 — 从 thread-local 取 session_id

> 📂 [`tools/skill_view_tool.py:37-52`](../../../tools/skill_view_tool.py#L37)

```python
def _current_session_id() -> str | None:
    """取当前线程绑定的 session_id（V26.2 ${SESSION_ID} 替换用）。

    复用 V25.1 的 thread-local 会话绑定——main loop 与
    delegate 子线程各自隔离。未绑定时返回 None，
    让 substitute_tokens 走「无值原样保留」分支。
    """
    from agent.logging import get_log_session
    sid = get_log_session()
    return sid if sid and sid != "-" else None
```

### tier 3 资源不替换

`read_resource()` 返回原始内容——模板文件里的 `${...}` 可能是模板自身的语法（如 Jinja2 变量），不该被 nano 动。只有 `SKILL.md` 正文（tier 2）过 token 替换。

### 完整数据流

```
skill_view("plan") 被调用
  │
  ├─ _current_session_id()
  │   └─ get_log_session() → thread-local → "abc123" 或 None
  │
  ├─ loader.view("plan", session_id)
  │   ├─ meta.path.read_text()  → 原始 SKILL.md
  │   └─ substitute_tokens(content, skill_dir, session_id)
  │       ├─ ${SKILL_DIR}   → /Users/.../skills/plan
  │       ├─ ${SESSION_ID}  → "abc123" 或原样保留
  │       └─ ${OPENAI_API_KEY} → 不匹配正则，原样保留
  │
  └─ tool_result(output=替换后的正文, ...)
```

---

## 六、完整示例：plan skill 的 `${SKILL_DIR}`

> 📂 [`skills/plan/SKILL.md`](../../../skills/plan/SKILL.md)

plan skill 的 SKILL.md 末尾有这样一段：

```markdown
## Before you finish

Run through the review checklist bundled with this skill. Read it with:

skill_view("plan", "references/checklist.md")

The checklist lives at `${SKILL_DIR}/references/checklist.md` on disk (the
`${SKILL_DIR}` token is substituted with this skill's absolute directory when
the skill is loaded, so the path stays correct wherever the repo is cloned).
```

### agent 实际看到什么

调用 `skill_view("plan")` 后，`${SKILL_DIR}` 被替换为真实路径：

```
你的机器上：
  The checklist lives at /Users/qshf/my-project/nano_hermes_agent/skills/plan/references/checklist.md

同事机器上（clone 到不同路径）：
  The checklist lives at /home/xiaoming/project/skills/plan/references/checklist.md
```

同一份 SKILL.md，不同机器自动变成正确路径。skill 作者不用猜测用户的部署位置。

### 与 v26.0 的联动

`${SKILL_DIR}` 让 tier 3 资源引用不再依赖硬编码路径。v26.0 的 `linked_files` 告诉 agent "这个 skill 有哪些资源"，v26.2 的 `${SKILL_DIR}` 告诉 agent "这些资源在哪"——两条信息链闭合。

```
tier 1 → system prompt 索引：plan — 写 markdown 计划
tier 2 → skill_view("plan")：
         output: "...The checklist lives at /Users/.../skills/plan/references/checklist.md..."
         linked_files: {references: [references/checklist.md]}    ← v26.0
tier 3 → skill_view("plan", "references/checklist.md")：
         具体 checklist 内容
```

---

## 七、关键设计决策

| 决策 | 选项 | 没选 | 原因 |
|------|------|------|------|
| token 范围 | 固定白名单 2 个 | 开放任意 env 名 | 防 `${OPENAI_API_KEY}` 把密钥写进 context |
| 内联 shell | 完全不移植 | 移植但默认关闭（源项目） | 函数存在即攻击面；从源头杜绝"skill=任意代码执行" |
| 无值行为 | 原样保留 token | 替成空串 | 让作者排错——看到未替换的 token 就知道运行期没拿到值 |
| tier 3 替换 | 不替换 | 也替换 | 模板文件里的 `${...}` 可能是模板自身语法 |
| session_id 来源 | thread-local（复用 V25.1） | 新构造参数链 | 复用现有设施，不新增传递路径 |

---

## 八、裁剪（vs 源项目 131 行）

| 源项目有 | nano 做 | 理由 |
|----------|---------|------|
| `${HERMES_SKILL_DIR}` / `${HERMES_SESSION_ID}` | ✅ 去掉 `HERMES_` 前缀 | nano 是独立项目，前缀徒增噪音 |
| `` !`cmd` `` 内联 shell | ❌ 函数不移植 | 安全红线 |
| `metadata.hermes.config` 配置变量 | ❌ 不做 | nano 无 config 体系 |
| 交互式 secret capture | ❌ 不做 | 配置交给用户改 `.env`（v26.1 同） |

---

## 附录：涉及文件一览

| 文件 | 涉及内容 |
|------|----------|
| [`agent/skill_preprocessing.py`](../../../agent/skill_preprocessing.py) | 核心：`substitute_tokens()` — 固定白名单正则 + 有值才替语义 |
| [`agent/skill_loader.py`](../../../agent/skill_loader.py) | `view()` 加 `session_id` 参数，返回前调 `substitute_tokens` |
| [`tools/skill_view_tool.py`](../../../tools/skill_view_tool.py) | `_current_session_id()` 从 thread-local 取 session_id 传给 `view()` |
| [`skills/plan/SKILL.md`](../../../skills/plan/SKILL.md) | 用上 `${SKILL_DIR}` 引用 `references/checklist.md`，作为真实示例 |
| [`scripts/test_v26_2_token_subst.py`](../../../scripts/test_v26_2_token_subst.py) | 8 项不变量：5 项 token 替换 + 3 项接入验证 |
| [`docs/decisions/v26.2.md`](../../../docs/decisions/v26.2.md) | 完整决策记录 |
| [`docs/Skill-system/skill-system-completion-plan.md`](../../../docs/Skill-system/skill-system-completion-plan.md) | §7 原始设计 |
