"""V21.3 / V26.0: ``skill_view`` 工具 — progressive disclosure tier 2/3 入口。

两种模式（设计理由见 docs/decisions/v26.0.md 与 v21.3 决策日志）：

- **无 ``file_path``（tier 2）**：读 SKILL.md，结果附 ``linked_files`` 列出
  该 skill 携带的 references/templates/assets/scripts。
- **有 ``file_path``（tier 3）**：读那个 bundled 资源；``read_resource`` 的两道
  沙箱防线拦 ``..`` 与 symlink 逃逸，binary 文件只回尺寸标记。

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


def _current_session_id() -> str | None:
    """取当前线程绑定的 session_id（V26.2 ${SESSION_ID} 替换用）。

    复用 V25.1 的 thread-local 会话绑定；未绑定（``"-"`` 哨兵）归一成 None。
    """
    try:
        from agent.logging import get_log_session

        sid = get_log_session()
        return sid if sid and sid != "-" else None
    except Exception:
        return None


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
        "Load a skill's content (progressive disclosure tier 2/3). "
        "Use this after seeing a skill's name and description in the system "
        "prompt's 'available skills' section, when you decide its detailed "
        "instructions are relevant to the current task. "
        "Without 'file_path' it returns the full SKILL.md (tier 2); the result "
        "lists any bundled 'linked_files' the skill carries. "
        "With 'file_path' it returns that one linked resource (tier 3), e.g. a "
        "reference doc or template. The content is in the 'output' field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name from the system-prompt index (e.g. 'plan').",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "OPTIONAL: a linked file within the skill, relative to its "
                    "directory (e.g. 'references/api.md'). Omit to read SKILL.md "
                    "and discover available linked files."
                ),
            },
        },
        "required": ["name"],
    },
}


def skill_view_handler(args: dict) -> str:
    name = (args.get("name") or "").strip()
    file_path = (args.get("file_path") or "").strip()
    if not name:
        return tool_error("Parameter 'name' is required.")

    if _skill_loader is None:
        return tool_error("skill_view: skill loader not initialized (no skills mounted).")

    # ── tier 3：读单个 bundled 资源 ───────────────────────────────────
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

    # ── tier 2：读 SKILL.md，末尾附 linked_files 引导 ─────────────────
    try:
        # V26.2：取当前会话 id 让 ${SESSION_ID} 也能替换（thread-local 隔离）
        session_id = _current_session_id()
        content = _skill_loader.view(name, session_id)
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
    # V26.1 可用性回填 —— 让 agent 在读完 SKILL.md 后立刻知道这个 skill 能不能用，
    # 缺哪些 env。字段名与源项目对齐（readiness_status: available | setup_needed）。
    meta = _skill_loader.get(name) if hasattr(_skill_loader, "get") else None
    missing = list(meta.missing_env_vars()) if meta is not None else []
    payload["readiness_status"] = "setup_needed" if missing else "available"
    if missing:
        payload["missing_env_vars"] = missing
        payload["setup_needed"] = True
    return tool_result(payload)


def _check_skill_loader_ready() -> bool:
    """check_fn — 工具只在 loader 已注入且至少有 1 个 skill 时暴露给 LLM。

    没有 skill 时把工具藏起来：避免 LLM 看到一个永远返回 error 的工具产生
    困惑。``/skill list`` 仍可工作（slash 命令旁路 LLM）。
    """
    return _skill_loader is not None and len(_skill_loader) > 0


registry.register(SKILL_VIEW_SCHEMA, skill_view_handler, check_fn=_check_skill_loader_ready)
