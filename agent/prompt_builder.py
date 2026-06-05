"""V21.2 → V23.2: 三段式 + 项目上下文段的系统 prompt 拼装器。

为什么要做（V21.2）
==================
V4 引入的 ``build_system_prompt`` 是 10 行 ``str.format`` 替换 — 名义上叫
"系统 prompt 构建器"但**没有任何分段抽象**。要再加一段（例如 V21.3 的
skill 索引、未来的安全护栏、风格约束），唯一扩展点是改 ``SYSTEM_PROMPT``
模板字符串和加新参数，模板里看不出"这一段是干什么的"。

V23.2 的扩展
============
跑 agent 时支持 ``--cwd <PATH>``（main.py 解析后传进来），这一档让 system
prompt 自动注入"用户项目根目录的上下文文件"：

- 文件优先级：``nano-hermes-agent.md`` → ``AGENTS.md``（首个命中即停）
- cwd 不传则用 ``os.getcwd()``；cwd 不存在 / 文件不存在均静默跳过
- 段位置：骨架之后、skill 索引之前 — 这是身份段的延伸
- 内容封顶 20000 字符（头 60% + 尾 30% + 中间截断标记），仿源项目
  ``agent/prompt_builder.py:1292`` 的 ``_truncate_content``
- env ``NANO_IGNORE_RULES=1`` 时整段跳过（仿源项目 ``HERMES_IGNORE_RULES``）

与源项目的差异（教学版有意简化）
============================
- 不走到 git root（源项目对 ``.hermes.md`` 会一路上溯）— nano 只看 cwd
- 不做 prompt-injection 扫描（源项目 ``_scan_context_content``）
- 不收 ``CLAUDE.md`` / ``.cursorrules`` 兜底 — 用户明确选了 nano 自己的命名
- 不做子目录渐进发现（源项目 ``subdirectory_hints.py``）

设计要点
========
- ``PromptBuilder.build()`` 返回 ``"\\n\\n".join(...)`` — 段间用空行分隔
- 每段都是独立方法 ``_render_<section>``，返回 str（空 str 表示该段跳过）
- 段顺序固定：骨架 → 项目上下文（V23.2）→ skill 索引 → memory → 工具列表
- ``cwd`` 为 None 时项目上下文段返回 ""，与 V21.2 行为完全一致（向下兼容）

关于 V20 prompt cache 的影响
==========================
段顺序固定是"上下文经济学"的关键：system prompt 必须在 V20 prompt cache
打 ephemeral 标记的位置稳定。V23.2 把项目上下文段插在骨架之后会改变
prefix 字节 — 但**同一个 cwd 内 prefix 仍稳定**，cache 命中不受影响；
切换 cwd 会让 prefix 失配，这是预期行为（不同项目本就不该共享 cache）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


SKELETON_PROMPT = """You are a helpful coding assistant.

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user."""


# 项目上下文文件配置
PROJECT_CONTEXT_FILE_NAMES = ("nano-hermes-agent.md", "AGENTS.md")
PROJECT_CONTEXT_MAX_CHARS = 20_000
PROJECT_CONTEXT_TRUNCATE_HEAD_RATIO = 0.6
PROJECT_CONTEXT_TRUNCATE_TAIL_RATIO = 0.3


def _truncate_project_context(content: str, filename: str) -> str:
    """头 60% + 尾 30% + 中间截断标记 — 仿源项目 _truncate_content。"""
    if len(content) <= PROJECT_CONTEXT_MAX_CHARS:
        return content
    head_chars = int(PROJECT_CONTEXT_MAX_CHARS * PROJECT_CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(PROJECT_CONTEXT_MAX_CHARS * PROJECT_CONTEXT_TRUNCATE_TAIL_RATIO)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    marker = (
        f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of "
        f"{len(content)} chars. Use file tools to read the full file.]\n\n"
    )
    return head + marker + tail


def _load_project_context(cwd: Path) -> str:
    """从 cwd 按优先级查找上下文文件，第一个命中即停。

    返回带 ``## <filename>`` 标题的段落文本；都不命中返回 ""。
    cwd 本身不存在 / 不是目录 / 文件读取失败 — 静默返回 ""，不阻塞 agent 启动。
    """
    if not cwd.exists() or not cwd.is_dir():
        return ""
    for name in PROJECT_CONTEXT_FILE_NAMES:
        candidate = cwd / name
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not content:
            continue
        body = _truncate_project_context(content, name)
        return f"## {name}\n\n{body}"
    return ""


class PromptBuilder:
    """三段式（V21.2）+ 项目上下文段（V23.2）的系统 prompt 拼装器。

    构造期接收所有渲染依赖（toolset 解析器、memory_manager、skill_loader、
    cwd），每次 ``build()`` 调用时各段独立渲染再 join。空段自动跳过。

    Parameters
    ----------
    get_toolset_tool_names :
        回调，传入 ``enabled_toolsets`` 列表返回内置 toolset 工具名集合
    enabled_toolsets :
        当前启用的 toolset 列表（如 ``["core"]``）
    memory_manager :
        ``MemoryManager`` 实例 — 用于提供 memory 段 + provider 工具名
    skill_loader :
        V21.3 起注入 ``SkillLoader``；V21.2 阶段始终为 None
    skeleton :
        骨架 prompt 文本，默认用模块常量 ``SKELETON_PROMPT``
    cwd :
        V23.2 项目上下文段的扫描根目录。``None`` 时整段跳过（V21.x 兼容）
    """

    def __init__(
        self,
        *,
        get_toolset_tool_names: Callable[[Iterable[str]], list[str]],
        enabled_toolsets: list[str],
        memory_manager: Any,
        skill_loader: Optional[Any] = None,
        skeleton: str = SKELETON_PROMPT,
        cwd: Optional[Path] = None,
    ) -> None:
        self._get_toolset_tool_names = get_toolset_tool_names
        self._enabled_toolsets = list(enabled_toolsets)
        self._memory_manager = memory_manager
        self._skill_loader = skill_loader
        self._skeleton = skeleton
        self._cwd = Path(cwd) if cwd is not None else None

    # ── 段渲染（每个返回 str；空 str 表示该段跳过）────────────────────

    def _render_skeleton(self) -> str:
        """身份 / 风格 / 安全约束 — 永远第一段，永远不为空。"""
        return self._skeleton.strip()

    def _render_project_context(self) -> str:
        """V23.2: 用户项目根目录的上下文文件（nano-hermes-agent.md / AGENTS.md）。

        cwd 为 None 时返回空（V21.x 兼容，单测路径不需要此段）。
        env ``NANO_IGNORE_RULES=1`` 时也整段跳过。
        """
        if self._cwd is None:
            return ""
        if os.environ.get("NANO_IGNORE_RULES", "0") not in ("0", "false", "False", ""):
            return ""
        return _load_project_context(self._cwd)

    def _render_skill_index(self) -> str:
        """V21.3 tier 1 skill 索引（仅 name + description）。

        V26.1 可用性门控：把当前可用工具/toolset 传给 ``list_metadata``，
        ``requires_tools``/``requires_toolsets`` 不满足的 skill **硬隐藏**
        （根本不进索引，省 token）；``required_env_vars`` 缺失的 skill 仍进
        索引但追加 ``⚠ (setup: set X)`` **软标记**，给 agent「引导用户配置」
        的机会。可用工具名复用 ``_render_tool_list`` 的同源逻辑，不新增依赖。
        """
        if self._skill_loader is None:
            return ""
        try:
            metadata = list(
                self._skill_loader.list_metadata(
                    available_tools=self._available_tool_names(),
                    available_toolsets=list(self._enabled_toolsets),
                )
            )
        except Exception:
            return ""
        if not metadata:
            return ""
        lines = ["## available skills"]
        for meta in metadata:
            suffix = ""
            # setup 软标记 —— 仅当 metadata 暴露该能力时（向后兼容旧 SkillMetadata）
            missing = getattr(meta, "missing_env_vars", lambda: [])()
            if missing:
                suffix = "  ⚠ (setup: set " + ", ".join(missing) + ")"
            lines.append(f"- {meta.name}: {meta.description}{suffix}")
        return "\n".join(lines)

    def _available_tool_names(self) -> list[str]:
        """当前可用工具名 —— 与 ``_render_tool_list`` 同源（toolset 内置 +
        memory provider 暴露的工具）。供 skill 索引的 requires_tools 门控用。"""
        builtin = list(self._get_toolset_tool_names(self._enabled_toolsets))
        provider = list(self._memory_manager.get_all_tool_names())
        return sorted(set(builtin + provider))

    def _render_memory_block(self) -> str:
        """复用 ``MemoryManager.build_system_prompt()`` —— 它内部自己处理空态。"""
        block = self._memory_manager.build_system_prompt()
        return (block or "").strip()

    def _render_tool_list(self) -> str:
        """工具列表段 — toolset 内置工具 + 所有 memory provider 暴露的工具。"""
        builtin_tool_names = list(self._get_toolset_tool_names(self._enabled_toolsets))
        provider_tool_names = list(self._memory_manager.get_all_tool_names())
        all_names = sorted(set(builtin_tool_names + provider_tool_names))
        if not all_names:
            return ""
        lines = ["## available tools"]
        for name in all_names:
            lines.append(f"- `{name}`")
        return "\n".join(lines)

    # ── 公共入口 ────────────────────────────────────────────────────

    def build(self) -> str:
        """渲染所有段并以单空行连接；空段自动跳过。"""
        sections = [
            self._render_skeleton(),
            self._render_project_context(),
            self._render_skill_index(),
            self._render_memory_block(),
            self._render_tool_list(),
        ]
        return "\n\n".join(s for s in sections if s).strip()
