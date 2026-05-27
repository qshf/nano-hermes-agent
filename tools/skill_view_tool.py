"""V21.3: ``skill_view`` 工具 — progressive disclosure tier 2 入口。

行为
====
agent 看到 ``PromptBuilder`` 注入的 tier 1 索引（仅 name + description）
后，**当它判断需要某个 skill 的详细指令时**主动调这个工具。tool result
落进 messages → 下一轮推理就有了完整指令。这是上下文经济学的关键：
一个 30+ skill 的 agent 启动只占 1-2 KB，详细指令只在按需展开时进入。

注册时序问题
============
``tools/__init__.py`` 在 import 时自动 ``discover_tools()`` 触发本模块
``registry.register(...)``，但此时 ``main.py`` 还没构造 ``SkillLoader``
实例。解决方案：模块级 singleton + ``set_skill_loader()`` setter，
``main.py`` 创建完 loader 后回调注入；handler 调用前检查是否已设置。
未设置时返回 error JSON 而非崩 — 让"不挂 skill"的部署仍能跑。
"""

from __future__ import annotations

from typing import Optional

from tools.registry import registry
from tools.result import tool_error, tool_result


_skill_loader = None  # type: Optional["SkillLoader"]  # noqa: F821 (forward ref)


def set_skill_loader(loader) -> None:
    """main.py 在构造完 SkillLoader 后调用，把实例注入工具。

    设计选择：不在 import 期 try/except `from agent import SkillLoader`，
    避免 ``tools/`` 与 ``agent/`` 子包之间的隐式循环依赖。
    """
    global _skill_loader
    _skill_loader = loader


SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": (
        "Load full content of a named skill (progressive disclosure tier 2). "
        "Use this after seeing a skill's name and description in the system "
        "prompt's 'available skills' section, when you decide its detailed "
        "instructions are relevant to the current task. The full markdown "
        "(including frontmatter) is returned as the 'output' field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name from the system-prompt index (e.g. 'plan').",
            },
        },
        "required": ["name"],
    },
}


def skill_view_handler(args: dict) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return tool_error("Parameter 'name' is required.")

    if _skill_loader is None:
        return tool_error("skill_view: skill loader not initialized (no skills mounted).")

    try:
        content = _skill_loader.view(name)
    except KeyError:
        available = ", ".join(_skill_loader.names()) or "(none)"
        return tool_error(f"unknown skill '{name}'. available: {available}")
    except FileNotFoundError as exc:
        return tool_error(f"skill file vanished: {exc}")

    return tool_result(output=content)


def _check_skill_loader_ready() -> bool:
    """check_fn — 工具只在 loader 已注入且至少有 1 个 skill 时暴露给 LLM。

    没有 skill 时把工具藏起来：避免 LLM 看到一个永远返回 error 的工具产生
    困惑。``/skill list`` 仍可工作（slash 命令旁路 LLM）。
    """
    return _skill_loader is not None and len(_skill_loader) > 0


registry.register(SKILL_VIEW_SCHEMA, skill_view_handler, check_fn=_check_skill_loader_ready)
