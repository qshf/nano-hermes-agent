# Skill 系统补全 — 资源/参数/可用性三层增强迭代计划

> **缘起**：[voice-kit-api-provider-plan.md](voice-kit-api-provider-plan.md) §「Skill system 增强」(行 380-391) 点名当前 nano skill 系统「还只是说明书加载系统」，列了 6 个待补能力。本文档把这 6 点对照源项目 [hermes-agent](/Users/qshf/my-project/hermes-agent) 真实实现逐一落地，拆成 **3 档可独立验证的迭代**（v26.0 / v26.1 / v26.2），每档遵循「看得见的差异最少 + 不变量脚本兜底」。
>
> 主线衔接：skill 子系统的根基由 v21.x 交互层弧线奠定（见 [iteration-plan.md](iteration-plan.md)，已完成 v21.1–v21.4：progressive disclosure 两层加载）。本组三档是它的**纵深补强**，按项目「相关主题用大号.小号拆」的惯例落为 **v26 档组（v26.0 / v26.1 / v26.2）**，走独立的 `skill/` 分支前缀（与 `flywheel/` 数据飞轮主轴并行，互不打断已完成档的语义），与 voice-kit 独立包可并行推进。
>
> **版本号说明**：项目版本是全局按时间顺序的「档」计数器（v17–20 transport → v21 交互层 → v22 流式 → … → v25 飞轮），不按主题分号段。本组虽是 v21 skill 系统的延伸，但作为**下一个落地的档**取 v26（号必须大于当前 v25.1），而非回填一个比 25 小的号。

---

## 1. 现状盘点（v21.3 之后 skill 系统能做什么）

| 能力 | 现状 | 文件 |
|------|------|------|
| tier 1 索引（name + description）注入 system prompt | ✅ | [agent/prompt_builder.py:160-173](../../agent/prompt_builder.py#L160-L173) |
| tier 2 全文按需加载（`skill_view` 工具） | ✅ | [tools/skill_view_tool.py](../../tools/skill_view_tool.py) |
| `/skill list / view / reload` slash | ✅ | [cli/commands/skill.py](../../cli/commands/skill.py) |
| platform 过滤（darwin/linux/win32） | ✅ | [agent/skill_loader.py:91-108](../../agent/skill_loader.py#L91-L108) |
| frontmatter 解析（yaml，无 fallback） | ✅ | [agent/skill_loader.py:59-88](../../agent/skill_loader.py#L59-L88) |
| **bundled 资源（references/templates/assets）发现** | ❌ | — |
| **bundled 资源按需读取（tier 3）** | ❌ | — |
| **路径穿越防护（sandbox 在 skill 目录内）** | ❌ | — |
| **`scripts/` 发现（不自动执行）** | ❌ | — |
| **参数/变量替换（`${...}` token）** | ❌ | — |
| **`requires_env_vars` 可用性门控 + setup 提示** | ❌ | — |
| **`requires_tools/toolsets` 条件激活（隐藏不相关 skill）** | ❌ | — |

nano 当前 `SkillMetadata` 只有 4 个字段（name/description/path/platforms），`view()` 只能读 `SKILL.md` 一个文件——这正是「说明书加载系统」的天花板：skill 无法携带模板、脚本、参考资料，也无法声明「我需要 `OPENAI_API_KEY` 才能用」。

---

## 2. 源项目实现勘察（6 个待补点的真实出处）

> voice-kit-plan 行 384-389 列的 6 点，逐一对照 hermes 源码。**关键发现：源项目没有 `allowed-tools` 字段**——它的「工具门控」走的是 `requires_tools` / `fallback_for_tools` 条件激活，是「隐藏不相关 skill」而非「限制 skill 能用的工具」。这个事实修正了 voice-kit-plan 行 387 的措辞。

### 2.1 references/templates/assets 资源索引

源项目 [tools/skills_tool.py:1196-1256](/Users/qshf/my-project/hermes-agent/tools/skills_tool.py) 在 `skill_view` 返回时扫描 skill 目录的四个子目录，组装成 `linked_files` dict：

```python
if skill_dir:
    references_dir = skill_dir / "references"
    if references_dir.exists():
        reference_files = [str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")]
    templates_dir = skill_dir / "templates"   # *.md *.py *.yaml *.json *.tex *.sh (rglob)
    assets_dir = skill_dir / "assets"         # 任意文件 (rglob)
    scripts_dir = skill_dir / "scripts"       # *.py *.sh *.bash *.js *.ts *.rb (glob)

linked_files = {}
if reference_files: linked_files["references"] = reference_files
# ... templates / assets / scripts 同理
result["linked_files"] = linked_files or None
result["usage_hint"] = "To view linked files, call skill_view(name, file_path) ..."
```

**核心范式**：skill 不是单个 `SKILL.md`，是一个**目录包**。`SKILL.md` 是入口（tier 2），引用的资源是 tier 3，按需再展开。返回结果里 `linked_files` 告诉 agent「这个 skill 还带了哪些文件、用什么路径取」。

### 2.2 资源按需读取（tier 3）——源项目用 `skill_view(name, file_path)` 而非独立工具

voice-kit-plan 行 385 设想一个独立的 `skill_resource_read` 工具。**源项目没有独立工具**，而是给 `skill_view` 加了第二个可选参数 `file_path`（[skills_tool.py:1086-1185](/Users/qshf/my-project/hermes-agent/tools/skills_tool.py)、工具 schema 行 1473-1483）：

```python
# schema: file_path 可选——省略取 SKILL.md，给了就取那个 linked file
if file_path and skill_dir:
    if has_traversal_component(file_path):        # 拦 ".."
        return {"success": False, "error": "Path traversal ('..') is not allowed."}
    target_file = skill_dir / file_path
    traversal_error = validate_within_dir(target_file, skill_dir)  # resolve 后仍在目录内
    if traversal_error: return {"success": False, "error": traversal_error}
    if not target_file.exists():
        # 列出 available_files 分类清单，引导 agent 用正确路径
        return {"success": False, "available_files": {...}, "hint": "..."}
    content = target_file.read_text("utf-8")      # UnicodeDecodeError → 标记 binary
    return {"success": True, "file": file_path, "content": content}
```

**设计判断**：一个工具两种模式（无 `file_path`=读 SKILL.md，有=读资源）比两个工具更省 LLM 的工具选择负担，也让 tier 2→tier 3 是同一心智模型的延伸。nano 采纳这个判断。

### 2.3 路径穿越防护

源项目两道防线（`tools/path_security.py`）：`has_traversal_component(file_path)` 先拦字面量 `..`；`validate_within_dir(target, skill_dir)` 再对 **resolve 后的绝对路径**校验是否仍以 `skill_dir` 为前缀（拦符号链接逃逸）。binary 文件不返回内容，只返回 `[Binary file: name, size: N bytes]` 标记，避免污染 context。

### 2.4 scripts/ 发现（不自动执行）

源项目 [skills_tool.py:1228-1233](/Users/qshf/my-project/hermes-agent/tools/skills_tool.py) 把 `scripts/` 下的 `*.py *.sh *.bash *.js *.ts *.rb` 收进 `linked_files["scripts"]`——**只发现、只列路径，从不自动执行**。agent 要跑某脚本时，走它本就有的 terminal/execute 工具显式调用。voice-kit-plan 行 389「先不自动执行」与源项目行为一致。

### 2.5 参数/变量替换

源项目 [agent/skill_preprocessing.py](/Users/qshf/my-project/hermes-agent/agent/skill_preprocessing.py)（131 行）在返回 SKILL.md 内容前做两类替换：

```python
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID)\}")
# 只替换有具体值的 token，无值的原样留下让作者排错
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")  # !`date +%Y-%m-%d` → 执行取 stdout
```

**两个机制差异巨大的风险等级**：`${...}` 变量替换是纯字符串替换（安全）；`` !`cmd` `` 内联 shell 是**执行任意命令**（危险，源项目默认 `inline_shell=False` 关闭，需 config 显式开）。nano 教学版**只做安全的 `${...}` token 替换，绝不做内联 shell**（与 §6 安全护栏一致）。

### 2.6 requires_env_vars 可用性门控

源项目 [skills_tool.py:226-292](/Users/qshf/my-project/hermes-agent/tools/skills_tool.py) 从 frontmatter 读 `required_environment_variables`，结合当前 env 算出 `missing_required_environment_variables`，回填到 `skill_view` 结果的 `setup_needed` / `readiness_status`（`available` / `setup_needed`）。源项目还有交互式 secret capture 回调（nano 不做），但「声明依赖 → 检查缺失 → 标记状态」这条链是 nano 要的。

### 2.7 条件激活（修正 voice-kit-plan 的 allowed-tools 措辞）

源项目 [prompt_builder.py:960-985](/Users/qshf/my-project/hermes-agent/agent/prompt_builder.py) 的 `_skill_passes_conditions`：

```python
# requires: 需要的工具/toolset 不在 → 隐藏该 skill
for t in conditions.get("requires_tools", []):
    if t not in available_tools: return False
# fallback_for: 主工具已可用 → 隐藏这个兜底 skill
for t in conditions.get("fallback_for_tools", []):
    if t in available_tools: return False
```

声明位置：`metadata.hermes.{requires_tools, requires_toolsets, fallback_for_tools, fallback_for_toolsets}`（[skill_utils.py:287-301](/Users/qshf/my-project/hermes-agent/agent/skill_utils.py)）。这是**索引层过滤**（不相关 skill 不出现在 tier 1），不是「限制 skill 激活后能用的工具」。nano 取其中 `requires_*` 子集做可用性门控。

---

## 3. 完整 frontmatter 字段集对照

| 字段 | hermes | nano 现状 | 本组三档 |
|------|--------|-----------|---------|
| `name` | ✅ | ✅ | — |
| `description` | ✅ | ✅ | — |
| `platforms` | ✅ | ✅ | — |
| `metadata.category` | ✅ | 解析但未用 | — |
| `required_environment_variables` | ✅ | ❌ | **v26.1** |
| `metadata.requires_tools/toolsets` | ✅ | ❌ | **v26.1** |
| `${HERMES_SKILL_DIR}` 等 token | ✅ | ❌ | **v26.2** |
| `metadata.hermes.config` / secret capture | ✅ | ❌ | 不做 |
| `required_credential_files` | ✅ | ❌ | 不做 |
| `allowed-tools` | **不存在** | — | 不做 |

目录结构：hermes `skills/<category>/<name>/`（两级，`os.walk` 递归），nano 保持 `skills/<name>/`（一级，`glob("*/SKILL.md")`）——本组三档**不改目录层级**，资源子目录直接挂在 `skills/<name>/{references,templates,assets,scripts}/`。

---

## 4. 三档拆分总览

| 版本 | 标题 | 核心概念 | 真跑验证 | 对应源项目 |
|------|------|---------|---------|-----------|
| **v26.0** | bundled 资源发现 + tier 3 读取 + 路径沙箱 | skill 从「单文件」升级为「目录包」；`skill_view(name, file_path)` 双模式；`..`/symlink 双防线；scripts 只发现不执行 | `/skill view plan` 末尾列出 linked_files；agent 读 `references/x.md`；`../` 被拦 | `skills_tool.py:1086-1256` + `path_security.py` |
| **v26.1** | 可用性门控（env vars + requires_tools） | frontmatter 声明 `required_environment_variables` / `requires_tools`；缺失则索引隐藏 + `skill_view` 标 `setup_needed` | 缺 env 的 skill 不进 tier 1 索引；`/skill list` 显示 ⚠ setup_needed | `skills_tool.py:226-292` + `prompt_builder.py:960-985` |
| **v26.2** | 参数/变量替换（仅安全 token） | `${SKILL_DIR}` / `${SESSION_ID}` 替换；**绝不做内联 shell** | SKILL.md 里 `${SKILL_DIR}` 在 view 输出中被替成绝对路径 | `skill_preprocessing.py:37-60`（只取 token 子集） |

**为什么拆三档不合一档**：三者风险等级递增——资源读取是纯文件 IO（v26.0，最安全先做，且是其他两档的载体）；可用性门控引入 env/tool 检查逻辑（v26.1）；变量替换碰到「执行」边界必须单独把安全红线讲清楚（v26.2）。合一档会让「这一档解决了什么」模糊，也让不变量脚本失去单档归属。

**为什么不更细**：每档都有独立可演示的差异 + 8-12 项不变量，已是最小可验证单元。再拆（如「v26.0a 只发现 v26.0b 才读」）会制造无功能的中间态。

---

## 5. v26.0 详细设计 — bundled 资源发现 + tier 3 读取 + 路径沙箱

### 5.1 解决的问题
skill 当前只能携带一个 `SKILL.md`。真实 skill 常需要带模板（`templates/report.md`）、参考资料（`references/api.md`）、脚本（`scripts/validate.py`）。没有资源层，skill 的指令只能把所有内容塞进 SKILL.md 正文，违背 progressive disclosure。

### 5.2 引入的概念

#### 5.2.1 `SkillMetadata` 增加 `skill_dir`
[agent/skill_loader.py](../../agent/skill_loader.py) 的 dataclass 加一个字段：

```python
@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: Path          # SKILL.md
    platforms: tuple[str, ...]
    skill_dir: Path      # 新增：SKILL.md 的父目录（资源扫描根）
```

`scan()` 里 `skill_dir=skill_md.parent` 一行即可。

#### 5.2.2 `SkillLoader.list_resources(name)` — tier 3 发现
新增方法，扫四个子目录返回 `linked_files` dict（仿源项目分类逻辑，扩展名白名单收紧）：

```python
_RESOURCE_DIRS = {
    "references": ("*.md", "*.txt"),
    "templates":  ("*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.sh"),
    "assets":     ("*",),                 # 任意文件（含 binary）
    "scripts":    ("*.py", "*.sh", "*.bash", "*.js", "*.ts"),
}

def list_resources(self, name: str) -> dict[str, list[str]]:
    meta = self._cache.get(name)
    if meta is None: raise KeyError(name)
    out: dict[str, list[str]] = {}
    for sub, patterns in _RESOURCE_DIRS.items():
        d = meta.skill_dir / sub
        if not d.is_dir(): continue
        files = sorted({str(f.relative_to(meta.skill_dir))
                        for pat in patterns for f in d.rglob(pat) if f.is_file()})
        if files: out[sub] = files
    return out
```

#### 5.2.3 `SkillLoader.read_resource(name, rel_path)` — tier 3 读取 + 沙箱
nano 不引入源项目的 `path_security.py` 整个模块，**内联两道防线**（教学场景看得见逻辑）：

```python
def read_resource(self, name: str, rel_path: str) -> tuple[str, bool]:
    """返回 (content, is_binary)。越界抛 ValueError，不存在抛 FileNotFoundError。"""
    meta = self._cache.get(name)
    if meta is None: raise KeyError(name)
    if ".." in Path(rel_path).parts:                       # 防线 1：字面量 ..
        raise ValueError("path traversal ('..') not allowed")
    target = (meta.skill_dir / rel_path).resolve()
    root = meta.skill_dir.resolve()
    if not (target == root or root in target.parents):     # 防线 2：resolve 后仍在目录内
        raise ValueError(f"path escapes skill dir: {rel_path}")
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    try:
        return target.read_text(encoding="utf-8"), False
    except UnicodeDecodeError:
        return f"[Binary file: {target.name}, {target.stat().st_size} bytes]", True
```

#### 5.2.4 `skill_view` 工具升级为双模式
[tools/skill_view_tool.py](../../tools/skill_view_tool.py) schema 加可选 `file_path`，handler 分流：

```python
# schema.properties 增加：
"file_path": {"type": "string",
    "description": "OPTIONAL: linked file within the skill "
                   "(e.g. 'references/api.md'). Omit to read SKILL.md."}

def skill_view_handler(args: dict) -> str:
    name = (args.get("name") or "").strip()
    file_path = (args.get("file_path") or "").strip()
    if not name: return tool_error("Parameter 'name' is required.")
    if _skill_loader is None: return tool_error("skill_view: loader not initialized.")
    if file_path:
        try:
            content, is_binary = _skill_loader.read_resource(name, file_path)
        except KeyError:   return tool_error(f"unknown skill '{name}'.")
        except ValueError as e:        return tool_error(str(e))
        except FileNotFoundError:
            avail = _skill_loader.list_resources(name)
            return tool_error(f"file '{file_path}' not found in '{name}'.",
                              available_files=avail)
        return tool_result(output=content, file=file_path, is_binary=is_binary)
    # 无 file_path：读 SKILL.md，末尾附 linked_files 引导
    content = _skill_loader.view(name)
    resources = _skill_loader.list_resources(name)
    payload = {"output": content}
    if resources:
        payload["linked_files"] = resources
        payload["usage_hint"] = ("To read a linked file, call skill_view again "
                                 "with file_path, e.g. skill_view(name, 'references/api.md').")
    return tool_result(payload)
```

`/skill view <name>` slash 也跟着在末尾打印 linked_files 清单（人工对照）。

### 5.3 裁剪（vs 源项目）
- 不抽独立 `path_security.py` 模块——两道防线内联进 `read_resource`（~6 行，教学直观）
- 不做 `assets/` 的 MIME 嗅探，binary 一律返回尺寸标记
- 不做 `index-cache` 磁盘快照（源项目对 100+ skill 必需）

### 5.4 不变量脚本 `scripts/test_v26_0_skill_resources.py`（目标 10 项）
**list_resources**（3）：无资源目录→空 dict；3 类资源→3 个 key 排序；扩展名不在白名单→不收。
**read_resource 沙箱**（4）：`../etc/passwd`→ValueError；`references/x.md` 正常读；不存在→FileNotFoundError + available_files；symlink 指向目录外→ValueError。
**skill_view 双模式**（3）：无 file_path→含 linked_files + usage_hint；有 file_path→只返回该文件；binary→is_binary=True 不含原始字节。

### 5.5 验证场景
- 建 `skills/plan/references/checklist.md` → `/skill view plan` 末尾列出 `references: [references/checklist.md]`
- 问 agent「按 plan skill 的 checklist 检查」→ 观察是否先 `skill_view("plan")` 看到 linked_files，再 `skill_view("plan", "references/checklist.md")`
- agent 调 `skill_view("plan", "../../../etc/passwd")` → 返回 error 不泄露

---

## 6. v26.1 详细设计 — 可用性门控（env vars + requires_tools）

> ✅ **已实现**（branch `skill/v0.26.1`，10/10 不变量）。落地详情见
> [docs/decisions/v26.1.md](../decisions/v26.1.md)。下文为原始设计，与实现一致。

### 6.1 解决的问题
voice-runtime 这类 skill「没有 `DASHSCOPE_API_KEY` 就用不了」，但当前 nano 会无差别把它注入 tier 1 索引，agent 调了才发现缺 key。需要：**声明依赖 → 启动期检查 → 缺失则索引隐藏（或标记）**，省下 token 也省下无效工具调用。

### 6.2 引入的概念

#### 6.2.1 frontmatter 新字段（nano 子集，命名去 HERMES 前缀）
```yaml
---
name: voice-runtime
description: "Speak progress updates via TTS."
required_environment_variables: [DASHSCOPE_API_KEY]   # 也支持 [{name, help}]
metadata:
  requires_tools: [terminal]        # 需要的工具不在 → 隐藏
  requires_toolsets: [core]
---
```

#### 6.2.2 `SkillMetadata` 增加门控字段 + readiness
```python
@dataclass(frozen=True)
class SkillMetadata:
    # ... 已有字段 ...
    required_env_vars: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()
    requires_toolsets: tuple[str, ...] = ()

    def missing_env_vars(self) -> list[str]:
        return [v for v in self.required_env_vars if not os.environ.get(v)]

    @property
    def setup_needed(self) -> bool:
        return bool(self.missing_env_vars())
```

`scan()` 从 frontmatter 抽这三个字段（`required_environment_variables` 顶层；`requires_*` 在 `metadata` 下）。

#### 6.2.3 索引层过滤 — `list_metadata` 接收可用工具集
`SkillLoader.list_metadata(available_tools=None, available_toolsets=None)`：仿源项目 `_skill_passes_conditions`，但 nano **只做 `requires_*`（不做 `fallback_for_*`，YAGNI）**。env 缺失的处理有两种策略：

| 策略 | 行为 | nano 选择 |
|------|------|----------|
| 硬隐藏 | 缺 env 的 skill 完全不进索引 | requires_tools 用这个 |
| 软标记 | 进索引但标 ⚠ setup_needed，agent 看到可提示用户配置 | env 缺失用这个 |

**决策**：env 缺失走**软标记**——因为 agent 可以引导用户「设置 X 后再用」，硬隐藏会让这个交互无从发生；requires_tools 不满足走**硬隐藏**——工具不存在时这个 skill 根本无法工作，留着只浪费 token。

```python
def list_metadata(self, available_tools=None, available_toolsets=None):
    at = set(available_tools or [])
    ats = set(available_toolsets or [])
    out = []
    for m in sorted(self._cache.values(), key=lambda x: x.name):
        if available_tools is not None:
            if any(t not in at for t in m.requires_tools):   continue  # 硬隐藏
            if any(s not in ats for s in m.requires_toolsets): continue
        out.append(m)   # env 缺失不在这里过滤——交给渲染层标记
    return out
```

#### 6.2.4 PromptBuilder / slash 渲染标记
[agent/prompt_builder.py:160-173](../../agent/prompt_builder.py#L160-L173) `_render_skill_index` 给 setup_needed 的 skill 加后缀：

```python
for meta in metadata:
    suffix = "  ⚠ (setup: set " + ", ".join(meta.missing_env_vars()) + ")" if meta.setup_needed else ""
    lines.append(f"- {meta.name}: {meta.description}{suffix}")
```

`skill_view` 工具结果也回填 `setup_needed` / `missing_env_vars` / `readiness_status`（`available` | `setup_needed`），与源项目字段名对齐。

### 6.3 PromptBuilder 需要拿到 available_tools
`_render_skill_index` 调 `list_metadata(available_tools=...)`，工具名从 builder 已持有的 `_get_toolset_tool_names(self._enabled_toolsets)` + provider 工具名取（与 `_render_tool_list` 同源）。**复用现有依赖，不新增构造参数。**

### 6.4 裁剪（vs 源项目）
- 不做交互式 secret capture 回调（源项目 `_capture_required_environment_variables`）——nano 只检查 + 标记，配置交给用户改 `.env`
- 不做 `required_credential_files` / `env_passthrough` / `register_credential_files`
- 不做 `fallback_for_tools/toolsets`（兜底语义，nano 场景用不到）
- 不做磁盘快照缓存键含 tools/platform

### 6.5 不变量脚本 `scripts/test_v26_1_availability.py`（目标 10 项）
**字段解析**（3）：顶层 `required_environment_variables` 抽出；`metadata.requires_tools` 抽出；缺字段→空 tuple 不报错。
**env 软标记**（3）：env 全在→setup_needed=False；缺一个→missing_env_vars 含它、setup_needed=True；仍出现在 list_metadata（软标记不过滤）。
**requires_tools 硬隐藏**（4）：tool 在 available→出现；tool 不在→不出现；available_tools=None（无信息）→全显示（向后兼容）；toolset 不在→不出现。

### 6.6 验证场景
- `skills/voice-runtime/SKILL.md` 声明 `required_environment_variables: [DASHSCOPE_API_KEY]`，不设该 env → `/skill list` 显示 `voice-runtime: ... ⚠ (setup: set DASHSCOPE_API_KEY)`
- 设了 env 重启 → ⚠ 消失
- skill 声明 `requires_tools: [nonexistent_tool]` → 不出现在 `/skill list` 也不进 system prompt 索引

---

## 7. v26.2 详细设计 — 参数/变量替换（仅安全 token）

> ✅ **已实现**（branch `skill/v0.26.2`，8/8 不变量）。落地详情见
> [docs/decisions/v26.2.md](../decisions/v26.2.md)。下文为原始设计，与实现一致。

### 7.1 解决的问题
skill 指令里常需引用「自己的目录」（让 agent 知道去哪读模板）或「当前会话 id」。硬编码绝对路径不可移植。源项目用 `${HERMES_SKILL_DIR}` token 解决。

### 7.2 引入的概念

#### 7.2.1 新增 `agent/skill_preprocessing.py`（~40 行，源项目 131 行的安全子集）
```python
import re
from pathlib import Path

# nano 去掉 HERMES_ 前缀，token 集合固定（白名单，不可扩展任意 env）
_TOKEN_RE = re.compile(r"\$\{(SKILL_DIR|SESSION_ID)\}")

def substitute_tokens(content: str, skill_dir: Path | None, session_id: str | None) -> str:
    """只替换有具体值的 token；无值的原样留下让作者排错。"""
    def _repl(m: re.Match) -> str:
        tok = m.group(1)
        if tok == "SKILL_DIR" and skill_dir: return str(skill_dir)
        if tok == "SESSION_ID" and session_id: return str(session_id)
        return m.group(0)        # 无值 → 不动
    return _TOKEN_RE.sub(_repl, content)
```

**与源项目最大的差异——绝不移植 `expand_inline_shell`**。源项目 `` !`cmd` `` 会执行任意 shell（默认关闭但存在）。nano 教学版连这个函数都不写进来，从源头杜绝「skill 文件 = 任意代码执行」的攻击面。这是 §8 安全护栏的硬约束。

#### 7.2.2 接入点：`view()` 与 `skill_view` 工具
`SkillLoader.view(name, session_id=None)` 在返回前过一遍 `substitute_tokens(content, meta.skill_dir, session_id)`。`skill_view` 工具 handler 从注入的运行期上下文取 session_id（v24 起 loader 可持有当前 session_id 引用，或工具调用时传入）。

**tier 3 资源文件不做替换**——只有 `SKILL.md` 正文过 token 替换；`read_resource` 返回原始内容（模板文件里的 `${...}` 可能是模板自身语法，不该被 nano 动）。

### 7.3 裁剪（vs 源项目）
- 不做内联 shell（安全红线，见上）
- 不做 `metadata.hermes.config` 配置变量替换（依赖 config.yaml 体系，nano 无）
- token 白名单固定 2 个，不开放任意 env 名替换（防止 `${OPENAI_API_KEY}` 这类把密钥写进 context）

### 7.4 不变量脚本 `scripts/test_v26_2_token_subst.py`（目标 8 项）
**token 替换**（5）：`${SKILL_DIR}`→绝对路径；`${SESSION_ID}` 有值→替换、无值→原样保留；未知 token `${FOO}`→原样；无 token 文本→不变；`${OPENAI_API_KEY}`→**不替换**（不在白名单，密钥不泄露）。
**接入**（3）：`view()` 输出含替换后的路径；`read_resource` 返回的资源**不**做替换；连续两个 token 都替换。

### 7.5 验证场景
- `skills/plan/SKILL.md` 写 `模板在 ${SKILL_DIR}/templates/plan.md` → `/skill view plan` 输出里 `${SKILL_DIR}` 被替成 `/Users/.../skills/plan`
- 写 `${OPENAI_API_KEY}` → 输出原样保留（确认白名单生效，密钥不进 context）

---

## 8. 安全护栏（贯穿三档）

| 风险 | 来源 | nano 防线 |
|------|------|----------|
| 路径穿越读任意文件 | tier 3 资源读取 | `..` 字面拦 + resolve 后前缀校验（v26.0） |
| 符号链接逃逸 | `assets/` 软链指向 `/etc` | resolve 后校验仍在 skill_dir（v26.0） |
| binary 污染 context | 读到图片/二进制 | 只返回尺寸标记不返回字节（v26.0） |
| **任意命令执行** | **源项目 `` !`cmd` `` 内联 shell** | **整个函数不移植**（v26.2） |
| 密钥写进 context | `${ENV_NAME}` 开放替换 | token 白名单固定 2 个（v26.2） |
| scripts 被自动跑 | `scripts/` 发现 | **只列路径，从不执行**（v26.0） |

---

## 9. 与 voice-kit 计划的衔接

本组三档完成后，[voice-kit-api-provider-plan.md](voice-kit-api-provider-plan.md) 设想的 `voice-runtime` skill 就能：
- 携带 `references/personality/default.md` 人格文档（v26.0 tier 3）
- 声明 `required_environment_variables: [DASHSCOPE_API_KEY]`，缺失时 `/skill list` 提示用户配置（v26.1）
- 在 SKILL.md 里用 `${SKILL_DIR}/references/personality/default.md` 引用人格文档（v26.2）

三档是 voice-kit 的**基础设施前置**，但彼此独立可验证——voice-kit 独立包的开发不必等这三档全部完成。

---

## 10. 决策一句话总结

| 决策 | 选项 | 没选 | 原因 |
|------|------|------|------|
| 资源读取入口 | 复用 `skill_view(name, file_path)` 双模式 | 独立 `skill_resource_read` 工具 | 源项目实证更省 LLM 工具选择负担；tier2→3 同一心智模型 |
| 路径沙箱 | 两道防线内联 read_resource | 移植 path_security.py 模块 | 教学场景 6 行看得见逻辑胜过黑盒模块 |
| env 缺失策略 | 软标记（进索引标 ⚠） | 硬隐藏 | agent 需要「引导用户配置」的交互机会 |
| requires_tools 策略 | 硬隐藏 | 软标记 | 工具不存在 skill 根本无法工作，留着浪费 token |
| 变量替换范围 | 固定 2 token 白名单 | 开放任意 env 名 | 防 `${API_KEY}` 把密钥写进 context |
| 内联 shell | **完全不移植** | 移植但默认关闭（源项目） | 教学版从源头杜绝「skill=任意代码执行」攻击面 |
| 目录层级 | 保持一级 `skills/<name>/` | 升级两级 `<category>/<name>/` | 本组聚焦资源/门控，不动既有结构 |
| allowed-tools 字段 | 不做（源项目本就没有） | 实现 voice-kit-plan 设想的字段 | 修正措辞：门控走 requires_tools 条件激活 |

完整的「为什么这样选 / 真实踩坑 / 验证方式」在每档落地后写入 `docs/decisions/v26.0.md` / `v26.1.md` / `v26.2.md`。
