"""V21.3: Skill loader — progressive disclosure tier 1 索引器。

为什么要做
==========
V21.2 把 ``PromptBuilder._render_skill_index`` 段位置占住了，但 ``skill_loader``
始终是 None → 该段始终空。本档把它填上：扫描 ``skills/<name>/SKILL.md``，
从 YAML frontmatter 抽 ``name`` / ``description`` / ``platforms`` 三个字段，
**只把这三件事注入 system prompt**（tier 1，几百 token）。完整 markdown 指令
（tier 2，几 KB）由 ``tools/skill_view_tool.py`` 在 agent 主动决定要用某个
skill 时通过工具调用拉取。

设计要点（仿源项目 ``agent/skill_utils.py`` 511 行 + ``tools/skills_tool.py``
扫描层 ~200 行的核心子集，去掉的部分：）
==================================================
- 不做嵌套子目录（源项目 ``skills/<category>/<name>/SKILL.md``，nano 一级）
- 不做条件激活（``requires_toolsets`` / ``requires_env_vars``）— 留给 v26.1
- 不做 mtime 失效检测的多级缓存（源项目对 100+ skill 是性能必需，nano 不需要）
- V26.0：tier 3 资源（``references/`` / ``templates/`` / ``assets/`` / ``scripts/``）
  已支持发现 + 沙箱读取（``list_resources`` / ``read_resource``）
- 不做 disabled list / external_dirs / qualified namespace
- 不做 fallback 解析 — yaml 解析失败直接抛 ``KeyError`` / ``yaml.YAMLError``

教学场景对错误的偏好是"显式失败 + 易排错"。

行为约定
========
- ``scan()`` 启动期 + ``/skill reload`` 时调；调用前清缓存
- ``list_metadata()`` 返回按 ``name`` 排序的 metadata 列表（PromptBuilder 用）
- ``view(name)`` 返回完整 markdown（含 frontmatter）；未知抛 ``KeyError``
- platform 不匹配的 skill 在 ``scan()`` 阶段被过滤；不会出现在索引也无法 view
- frontmatter 缺 ``name`` / ``description`` 抛 KeyError（教学场景显式失败）
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLATFORM_MAP: dict[str, str] = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}


@dataclass(frozen=True)
class SkillMetadata:
    """tier 1 元信息 — 注入 system prompt 的最小集。

    V26.0：新增 ``skill_dir`` —— SKILL.md 的父目录，作为 tier 3 资源
    （references / templates / assets / scripts）扫描与读取的沙箱根。

    V26.1：新增可用性门控字段（仿源项目 ``skill_utils.py`` 的
    ``required_environment_variables`` + ``metadata.requires_tools/toolsets``
    子集，去 HERMES 前缀）。两种门控策略不同：

    - ``required_env_vars`` 缺失 → **软标记**（仍进索引，渲染层标 ⚠ setup_needed），
      因为 agent 需要「引导用户配置」的交互机会；
    - ``requires_tools`` / ``requires_toolsets`` 不满足 → **硬隐藏**（不进索引），
      因为工具不存在时 skill 根本无法工作，留着只浪费 token。

    （nano **不做** 源项目的 ``fallback_for_tools/toolsets`` 兜底语义——YAGNI。）
    """

    name: str
    description: str
    path: Path
    platforms: tuple[str, ...]
    skill_dir: Path = Path(".")  # 资源扫描根（SKILL.md 父目录）
    required_env_vars: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()
    requires_toolsets: tuple[str, ...] = ()

    def missing_env_vars(self) -> list[str]:
        """声明的 env 中当前未设置（或为空串）的那些。顺序与声明一致。"""
        return [v for v in self.required_env_vars if not os.environ.get(v)]

    @property
    def setup_needed(self) -> bool:
        """有任一 required env 缺失 → 需要用户配置才能用（软标记依据）。"""
        return bool(self.missing_env_vars())


# tier 3 资源子目录 → 扩展名白名单。
# 与源项目 ``tools/skills_tool.py:1196-1256`` 分类一致，扩展名收紧：
# - references/templates 限文本类，scripts 限可执行脚本语言，assets 放行任意（含 binary）
# - 白名单之外的文件不被 list_resources 收录（但 read_resource 仍可显式读，沙箱不依赖白名单）
_RESOURCE_DIRS: dict[str, tuple[str, ...]] = {
    "references": ("*.md", "*.txt"),
    "templates": ("*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.sh"),
    "assets": ("*",),  # 任意文件（含 binary）
    "scripts": ("*.py", "*.sh", "*.bash", "*.js", "*.ts"),
}


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 ``---`` 包围的 YAML frontmatter。

    Returns:
        ``(frontmatter_dict, body)``；无 frontmatter 时 ``frontmatter_dict``
        为空 dict，``body`` 即原文。

    与源项目对照（``agent/skill_utils.py:52-86``）：
    - 用 ``yaml.SafeLoader``（PyYAML 自带），不区分 CSafeLoader（性能差异在
      百量级 skill 才显著，nano 教学场景不需要）
    - 不做 fallback 简单 ``key:value`` 解析 — yaml 报错直接冒泡到调用方
    """
    if not content.startswith("---"):
        return {}, content

    # 查找首个 "---" 之后下一行 "---"（结束分隔符）
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return {}, content

    yaml_text = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    import yaml  # 延迟 import：未配 skill 时不付加载代价

    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, dict):
        # frontmatter 是 list 或 scalar — 当作没有 frontmatter
        return {}, body
    return parsed, body


def skill_matches_platform(frontmatter: dict[str, Any]) -> bool:
    """当前 OS 是否匹配 skill 声明的 platforms。

    缺省或空 → 全平台兼容（向后兼容默认）。
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]

    current = sys.platform
    for p in platforms:
        normalized = str(p).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


def _parse_str_list(raw: Any) -> tuple[str, ...]:
    """把 frontmatter 里的字段规整成 ``tuple[str, ...]``。

    宽容输入：``None``/缺省→空 tuple；标量→单元素 tuple；list→逐项 str。
    用于 ``metadata.requires_tools`` / ``requires_toolsets``。
    """
    if not raw:
        return ()
    if not isinstance(raw, list):
        raw = [raw]
    return tuple(str(x).strip() for x in raw if str(x).strip())


def _parse_env_vars(fm: dict[str, Any]) -> tuple[str, ...]:
    """抽顶层 ``required_environment_variables``。

    支持两种写法（与源项目兼容）：
    - 纯名字列表：``[DASHSCOPE_API_KEY, OTHER_KEY]``
    - 带 help 的对象列表：``[{name: KEY, help: "..."}]`` —— nano 只取 ``name``
      （不做交互式 secret capture，help 文案留给作者写进 SKILL.md 正文）。
    """
    raw = fm.get("required_environment_variables")
    if not raw:
        return ()
    if not isinstance(raw, list):
        raw = [raw]
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        if name and str(name).strip():
            names.append(str(name).strip())
    return tuple(names)


class SkillLoader:
    """扫描 ``skills/<name>/SKILL.md`` → 缓存 metadata → 按需读全文。

    Three-tier progressive disclosure
    ==================================
    - tier 1 (cheap)：``list_metadata()`` 启动期遍历返回轻量元信息，
      由 ``PromptBuilder._render_skill_index`` 注入 system prompt
    - tier 2 (on-demand)：``view(name)`` 在 agent 决定要用某 skill 时
      通过 ``tools/skill_view_tool.py`` 触发；返回完整 markdown 内容
    - tier 3 (on-demand, V26.0)：``list_resources(name)`` 发现 bundled
      资源，``read_resource(name, rel_path)`` 在沙箱内按需读取单个资源文件

    Parameters
    ----------
    skills_dir : Path
        nano 一级目录约定：``skills/<name>/SKILL.md``。dir 不存在时
        ``scan()`` 会静默置缓存为空（启动 banner 仍能跑，仅没有 skill 段）
    """

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)
        self._cache: dict[str, SkillMetadata] = {}

    # ── tier 1：扫描 + 索引 ────────────────────────────────────────────

    def scan(self) -> None:
        """重建 metadata 缓存。``/skill reload`` 也调这个。"""
        self._cache.clear()
        if not self.skills_dir.is_dir():
            return

        # nano 一级结构：skills/<name>/SKILL.md
        for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                fm, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            except Exception as exc:
                # 单个 skill yaml 错误不应 take down agent 启动；
                # 打 warning 跳过让其他 skill 仍可加载
                print(f"  [skill] parse failed: {skill_md} ({exc})")
                continue

            if not skill_matches_platform(fm):
                continue

            # 教学场景：缺 name / description 显式失败而非默认值
            if "name" not in fm:
                raise KeyError(f"{skill_md}: frontmatter missing 'name'")
            if "description" not in fm:
                raise KeyError(f"{skill_md}: frontmatter missing 'description'")

            platforms_raw = fm.get("platforms") or []
            if not isinstance(platforms_raw, list):
                platforms_raw = [platforms_raw]
            platforms = tuple(str(p) for p in platforms_raw)

            meta = SkillMetadata(
                name=str(fm["name"]),
                description=str(fm["description"]).strip(),
                path=skill_md,
                platforms=platforms,
                skill_dir=skill_md.parent,  # 资源扫描根
                required_env_vars=_parse_env_vars(fm),
                requires_tools=_parse_str_list(
                    (fm.get("metadata") or {}).get("requires_tools")
                ),
                requires_toolsets=_parse_str_list(
                    (fm.get("metadata") or {}).get("requires_toolsets")
                ),
            )
            self._cache[meta.name] = meta

    def list_metadata(
        self,
        available_tools: list[str] | None = None,
        available_toolsets: list[str] | None = None,
    ) -> list[SkillMetadata]:
        """返回按 name 排序的 metadata 列表（V26.1 加可用性门控）。

        门控策略（仿源项目 ``prompt_builder._skill_passes_conditions`` 的
        ``requires_*`` 子集，nano 不做 ``fallback_for_*``）：

        - ``requires_tools`` / ``requires_toolsets`` 任一不满足 → **硬隐藏**
          （不进返回列表）：工具不存在 skill 无法工作，留着浪费 token。
        - ``required_env_vars`` 缺失 → **不在这里过滤**：交给渲染层软标记
          ⚠ setup_needed，给 agent「引导用户配置」的机会。

        向后兼容：``available_tools=None`` 表示「调用方没传工具信息」→ 不做
        硬隐藏，全显示（V21.x / 单测路径行为不变）。``available_toolsets``
        同理独立判断。
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

    # ── tier 2：按需读全文 ─────────────────────────────────────────────

    def view(self, name: str) -> str:
        """读取指定 skill 的完整 markdown（含 frontmatter）。

        Raises:
            KeyError: 未知 skill 名（不在缓存中）
            FileNotFoundError: 缓存里有但磁盘没了（目录被外部 mv 后未 reload）
        """
        meta = self._cache.get(name)
        if meta is None:
            raise KeyError(f"unknown skill: {name}")
        return meta.path.read_text(encoding="utf-8")

    # ── tier 3：bundled 资源发现 + 沙箱读取 ───────────────────

    def list_resources(self, name: str) -> dict[str, list[str]]:
        """发现某 skill 携带的 bundled 资源（references/templates/assets/scripts）。

        skill 不是单个 ``SKILL.md``，而是一个**目录包**：``SKILL.md`` 是入口
        （tier 2），引用的资源是 tier 3。本方法扫四个子目录，按类别返回相对
        ``skill_dir`` 的路径列表 —— 告诉 agent「这个 skill 还带了哪些文件、
        用什么路径取」。只发现、只列路径，**从不执行**（含 ``scripts/``）。

        Returns:
            ``{"references": ["references/api.md"], ...}``；无资源 → 空 dict。
            每类内部按路径排序、去重。

        Raises:
            KeyError: 未知 skill 名。
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

    def read_resource(self, name: str, rel_path: str) -> tuple[str, bool]:
        """读取 skill 目录内的 tier 3 资源；两道防线把读取沙箱在 skill_dir 内。

        nano 不引入源项目的 ``path_security.py`` 整个模块，把两道防线内联
        （教学场景看得见逻辑）：

        - 防线 1：字面量 ``..`` 拦截 —— 任何路径分量是 ``..`` 直接拒（最常见攻击）
        - 防线 2：``resolve()`` 后前缀校验 —— 解析符号链接后仍须落在 skill_dir
          内（拦 symlink 指向目录外的逃逸）

        Returns:
            ``(content, is_binary)``。文本文件返回原始内容；无法 utf-8 解码的
            binary 文件**不返回字节**，只返回 ``[Binary file: name, N bytes]``
            尺寸标记（避免污染 context）。

        Raises:
            KeyError: 未知 skill 名。
            ValueError: 路径越界（含字面 ``..`` 或 resolve 后逃逸）。
            FileNotFoundError: 路径在沙箱内但文件不存在。
        """
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

    # ── 便利访问器 ─────────────────────────────────────────────────────

    def get(self, name: str) -> "SkillMetadata | None":
        """按名取 metadata；未知返回 None（不抛，调用方按需判空）。"""
        return self._cache.get(name)

    def has(self, name: str) -> bool:
        return name in self._cache

    def names(self) -> list[str]:
        return sorted(self._cache.keys())

    def __len__(self) -> int:
        return len(self._cache)
