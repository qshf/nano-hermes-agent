# v26.0 Skill 子系统纵深补强 —— 知识点

## 一、背景

v26.0 之前，skill 只是单个 SKILL.md 文件。agent 通过 tier 1（system prompt 里的索引）知道有哪些 skill，通过 tier 2（`skill_view` 工具）拿到完整指令。但如果 skill 需要附带参考文档、模板、脚本等辅助资源，旧架构无能为力。

v26.0 把 skill 从**单文件升级为目录包**，并引入 tier 3：bundled 资源发现与按需读取。

---

## 二、数据结构变更

`SkillMetadata` 新增 `skill_dir` 字段，指向 SKILL.md 的父目录：

> 📂 [`agent/skill_loader.py:50-62`](../../../agent/skill_loader.py#L50)

```python
@dataclass(frozen=True)
class SkillMetadata:
    """tier 1 元信息 — 注入 system prompt 的最小集。

    V26.0：新增 ``skill_dir`` —— SKILL.md 的父目录，作为 tier 3 资源
    （references / templates / assets / scripts）扫描与读取的沙箱根。
    """
    name: str
    description: str
    path: Path
    platforms: tuple[str, ...]
    skill_dir: Path = Path(".")  # V26.0：资源扫描根（SKILL.md 父目录）
```

---

## 三、资源目录分类与白名单

四类资源子目录，每类限制扩展名：

> 📂 [`agent/skill_loader.py:69-74`](../../../agent/skill_loader.py#L69)

```python
_RESOURCE_DIRS: dict[str, tuple[str, ...]] = {
    "references": ("*.md", "*.txt"),
    "templates":  ("*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.sh"),
    "assets":     ("*",),           # 任意文件（含 binary）
    "scripts":    ("*.py", "*.sh", "*.bash", "*.js", "*.ts"),
}
```

- `references/`：纯文本参考文档（只收 .md / .txt）
- `templates/`：模板文件（代码模板、配置模板等）
- `assets/`：任意资源（图片等，放行所有扩展名）
- `scripts/`：可执行脚本语言（**只发现，不执行**）

---

## 四、tier 2 如何让 agent 知道有 tier 3 资源

agent 不需要猜。它调用 `skill_view(name)`（tier 2）时，返回值里**主动告知**：

> 📂 [`tools/skill_view_tool.py:110-127`](../../../tools/skill_view_tool.py#L110)

```python
# ── tier 2：读 SKILL.md，末尾附 linked_files 引导 ─────────────────
try:
    content = _skill_loader.view(name)
except KeyError:
    available = ", ".join(_skill_loader.names()) or "(none)"
    return tool_error(f"unknown skill '{name}'. available: {available}")
except FileNotFoundError as exc:
    return tool_error(f"skill file vanished: {exc}")

payload: dict = {"output": content}
resources = _skill_loader.list_resources(name)
if resources:
    payload["linked_files"] = resources
    payload["usage_hint"] = (
        "To read a linked file, call skill_view again with file_path, "
        "e.g. skill_view(name, 'references/api.md')."
    )
return tool_result(payload)
```

agent 实际看到的结构：

```yaml
output: |
  <SKILL.md 全文...>

linked_files:
  references:
    - references/api.md
    - references/roadmap.md
  templates:
    - templates/example.py

usage_hint: "To read a linked file, call skill_view again with file_path,
             e.g. skill_view(name, 'references/api.md')."
```

### 完整信息链

```
system prompt（tier 1）
  → "available skills: plan — 写 markdown 计划"

agent 感兴趣，调用 skill_view("plan")  ← tier 2
  → 拿到 SKILL.md 全文
  → 看到 linked_files："这个 skill 还带了 references/api.md"
  → 看到 usage_hint："想读的话这样调"

agent 需要深入，调用 skill_view("plan", "references/api.md")  ← tier 3
  → 拿到具体参考文档内容
```

---

## 五、`list_resources`：发现 bundled 资源

> 📂 [`agent/skill_loader.py:213-247`](../../../agent/skill_loader.py#L213)

```python
def list_resources(self, name: str) -> dict[str, list[str]]:
    """发现某 skill 携带的 bundled 资源（references/templates/assets/scripts）。

    skill 不是单个 ``SKILL.md``，而是一个**目录包**：``SKILL.md`` 是入口
    （tier 2），引用的资源是 tier 3。本方法扫四个子目录，按类别返回相对
    ``skill_dir`` 的路径列表 —— 告诉 agent「这个 skill 还带了哪些文件、
    用什么路径取」。只发现、只列路径，**从不执行**（含 ``scripts/``）。
    """
    meta = self._cache.get(name)
    if meta is None:
        raise KeyError(f"unknown skill: {name}")

    out: dict[str, list[str]] = {}
    for sub, patterns in _RESOURCE_DIRS.items():
        d = meta.skill_dir / sub
        if not d.is_dir():
            continue
        files = sorted(
            {
                str(f.relative_to(meta.skill_dir))
                for pat in patterns
                for f in d.rglob(pat)
                if f.is_file()
            }
        )
        if files:
            out[sub] = files
    return out
```

遍历四类子目录，按扩展名白名单 `rglob` 递归收集，返回相对 `skill_dir` 的路径列表。**只发现、不执行**（含 `scripts/`）。

---

## 六、`read_resource`：沙箱内按需读取

**沙箱 = 把文件读取范围关在一个笼子里**——agent 只能读到 `skill_dir` 以内的文件，读不到外面的任何东西。

> 📂 [`agent/skill_loader.py:249-290`](../../../agent/skill_loader.py#L249)

```python
def read_resource(self, name: str, rel_path: str) -> tuple[str, bool]:
    """读取 skill 目录内的 tier 3 资源；两道防线把读取沙箱在 skill_dir 内。"""
    meta = self._cache.get(name)
    if meta is None:
        raise KeyError(f"unknown skill: {name}")

    # 防线 1：字面量 ".." —— 在 resolve 前先拦，错误信息最直观
    if ".." in Path(rel_path).parts:
        raise ValueError(f"path traversal ('..') not allowed: {rel_path}")

    target = (meta.skill_dir / rel_path).resolve()
    root = meta.skill_dir.resolve()
    # 防线 2：resolve 后仍须在 skill_dir 内（拦 symlink 逃逸）
    if not (target == root or root in target.parents):
        raise ValueError(f"path escapes skill dir: {rel_path}")

    if not target.is_file():
        raise FileNotFoundError(rel_path)

    try:
        return target.read_text(encoding="utf-8"), False
    except UnicodeDecodeError:
        size = target.stat().st_size
        return f"[Binary file: {target.name}, {size} bytes]", True
```

### 防线 1：字面量 `..` 拦截

最经典的路径穿越攻击——agent 试图往上跳出 `skill_dir`：

```python
# agent 传：skill_view("plan", "../../etc/passwd")

if ".." in Path(rel_path).parts:          # L274
    raise ValueError(...)                 # → 直接拒，连 resolve 都不走
```

### 防线 2：symlink 逃逸拦截

攻击者在 skill 目录内放了指向外部的符号链接：

```bash
skills/plan/references/secret → /etc/passwd
```

```python
# agent 传：skill_view("plan", "references/secret")

target = (meta.skill_dir / rel_path).resolve()   # L277 → /etc/passwd
root = meta.skill_dir.resolve()                  # L278 → skills/plan/

if not (target == root or root in target.parents):  # L280
    raise ValueError(...)   # → /etc/passwd 不在 skills/plan/ 下 → 拒
```

### 效果对比

```
不加沙箱：
  skill_view("plan", "../../../etc/passwd")     → 系统文件泄露
  skill_view("plan", "references/symlink_out")  → 任意文件泄露

加沙箱后：
  skill_view("plan", "../../../etc/passwd")     → ValueError ❌
  skill_view("plan", "references/symlink_out")  → ValueError ❌
  skill_view("plan", "references/api.md")       → ✅ 正常返回
```

### binary 文件处理

遇到无法 UTF-8 解码的文件（图片等），不把字节塞进 context，只回尺寸标记：

```python
# L286-290
try:
    return target.read_text(encoding="utf-8"), False
except UnicodeDecodeError:
    size = target.stat().st_size
    return f"[Binary file: {target.name}, {size} bytes]", True  # 不污染 context
```

---

## 七、`skill_view` 工具双模式

> 📂 [`tools/skill_view_tool.py:83-127`](../../../tools/skill_view_tool.py#L83)

```python
def skill_view_handler(args: dict) -> str:
    name = (args.get("name") or "").strip()
    file_path = (args.get("file_path") or "").strip()
    if not name:
        return tool_error("Parameter 'name' is required.")

    if _skill_loader is None:
        return tool_error("skill_view: skill loader not initialized (no skills mounted).")

    # ── tier 3：读单个 bundled 资源 ──────────────────  [L92-108]
    if file_path:
        try:
            content, is_binary = _skill_loader.read_resource(name, file_path)
        except KeyError:
            available = ", ".join(_skill_loader.names()) or "(none)"
            return tool_error(f"unknown skill '{name}'. available: {available}")
        except ValueError as exc:
            # 路径越界（.. 或 symlink 逃逸）—— 不泄露任何文件内容
            return tool_error(str(exc))
        except FileNotFoundError:
            avail = _skill_loader.list_resources(name)
            return tool_error(
                f"file '{file_path}' not found in skill '{name}'.",
                available_files=avail or None,
            )
        return tool_result(output=content, file=file_path, is_binary=is_binary)

    # ── tier 2：读 SKILL.md，末尾附 linked_files 引导 ──  [L110-127]
    try:
        content = _skill_loader.view(name)
    except KeyError:
        available = ", ".join(_skill_loader.names()) or "(none)"
        return tool_error(f"unknown skill '{name}'. available: {available}")
    except FileNotFoundError as exc:
        return tool_error(f"skill file vanished: {exc}")

    payload: dict = {"output": content}
    resources = _skill_loader.list_resources(name)
    if resources:
        payload["linked_files"] = resources
        payload["usage_hint"] = (
            "To read a linked file, call skill_view again with file_path, "
            "e.g. skill_view(name, 'references/api.md')."
        )
    return tool_result(payload)
```

---

## 八、关键设计决策

| 决策 | 理由 |
|------|------|
| 一个工具双模式，不拆两个 | 减少 LLM 的工具选择负担 |
| 只发现、不执行 | `scripts/` 也只列路径，agent 不能直接跑脚本 |
| 两道防线内联在 `read_resource` | 教学场景看得见逻辑，不引入外部安全模块 |
| binary 只回尺寸标记 | 避免污染 context（一张图进消息没意义） |
| 资源目录白名单按扩展名分类 | 防止 agent 读到 `.env` / `.key` / 二进制等意外文件 |

---

## 九、完整示例：`code-reading` skill

以上机制的一个具体实现 — 项目中有一个 `code-reading` skill，结构如下：

```
skills/code-reading/
├── SKILL.md                          ← 四阶段阅读法（入口 + 核心）
└── references/
    └── tracing-guide.md              ← grep 技巧、架构模式速查、异步追踪陷阱
```

它在运行时经历的 tier 1/2/3 流程：

```
tier 1 — system prompt 索引：
  "code-reading: 系统性阅读源码：定位入口、追踪数据流、识别模式、验证理解"

tier 2 — agent 调用 skill_view("code-reading")：
  → output: 四阶段阅读法全文（定位入口→追踪数据流→识别模式→验证理解）
  → linked_files: {"references": ["references/tracing-guide.md"]}
  → usage_hint: "想深入读调用 skill_view(name, 'references/tracing-guide.md')"

tier 3 — agent 调用 skill_view("code-reading", "references/tracing-guide.md")：
  → output: grep 追踪命令、ASCII 调用图方法、6 种架构模式速查表、
            异步/多线程追踪陷阱、git blame 三连击、卡住时的解法
```

> 📂 实物见 [`skills/code-reading/`](../../../skills/code-reading/) — SKILL.md + references/tracing-guide.md

---

## 附录：涉及文件一览

| 文件 | 涉及内容 |
|------|----------|
| [`agent/skill_loader.py`](../../../agent/skill_loader.py) | `SkillMetadata` (L50-62)、`_RESOURCE_DIRS` (L69-74)、`list_resources` (L213-247)、`read_resource` (L249-290) |
| [`tools/skill_view_tool.py`](../../../tools/skill_view_tool.py) | `skill_view_handler` 双模式 (L83-127)、工具 schema (L50-80) |
| [`cli/commands/skill.py`](../../../cli/commands/skill.py) | `/skill view` 末尾列 tier 3 资源 |
| [`skills/code-reading/`](../../../skills/code-reading/) | 完整 skill 示例：SKILL.md + references/tracing-guide.md |
