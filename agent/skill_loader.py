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
- 不做条件激活（``requires_toolsets`` / ``requires_env_vars``）
- 不做 mtime 失效检测的多级缓存（源项目对 100+ skill 是性能必需，nano 不需要）
- 不做 ``references/`` / ``templates/`` 子文件（tier 3）
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
    """tier 1 元信息 — 注入 system prompt 的最小集。"""

    name: str
    description: str
    path: Path
    platforms: tuple[str, ...]


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


class SkillLoader:
    """扫描 ``skills/<name>/SKILL.md`` → 缓存 metadata → 按需读全文。

    Two-tier progressive disclosure
    ===============================
    - tier 1 (cheap)：``list_metadata()`` 启动期遍历返回轻量元信息，
      由 ``PromptBuilder._render_skill_index`` 注入 system prompt
    - tier 2 (on-demand)：``view(name)`` 在 agent 决定要用某 skill 时
      通过 ``tools/skill_view_tool.py`` 触发；返回完整 markdown 内容

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
            )
            self._cache[meta.name] = meta

    def list_metadata(self) -> list[SkillMetadata]:
        """返回按 name 排序的 metadata 列表。"""
        return sorted(self._cache.values(), key=lambda m: m.name)

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

    # ── 便利访问器 ─────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        return name in self._cache

    def names(self) -> list[str]:
        return sorted(self._cache.keys())

    def __len__(self) -> int:
        return len(self._cache)
