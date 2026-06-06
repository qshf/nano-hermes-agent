"""``/skill`` — V21.3 progressive disclosure 入口。

子命令
======
- ``/skill list``         — 列出已加载 skill 的 name + description（tier 1 视图）
- ``/skill view <name>``  — 打印指定 skill 完整 markdown（tier 2，等价于
  agent 调 ``skill_view`` 工具，便于人工对照检查）
- ``/skill reload``       — 重扫 ``skills/`` 目录；改完 SKILL.md 不用重启 agent

实现取舍
========
- 不实现 ``/skill enable`` / ``/skill disable``：源项目通过 manifest enable
  list 控制可见；nano 第一档故意让所有解析成功的 skill 都可见，把"启用控制"
  推迟到后续版本（YAGNI）。
- ``view`` 直接打印到 stdout — 教学场景下用户在终端直接看；超长 skill 不分页，
  避免内嵌 pager（pager 不可移植，且会破坏单元测试用 capsys 捕获）。
"""

from __future__ import annotations

from cli.context import AgentCtx
from cli.registry import command, split_args


def _resolve_loader(ctx: AgentCtx):
    """从 ctx 取 skill_loader；没挂时返回 None 让 handler 给统一文案。"""
    return getattr(ctx, "skill_loader", None)


@command(
    "/skill",
    description="List/view/reload skills (progressive disclosure)",
    category="skills",
    args_hint="<list|view <name>|reload>",
)
def cmd_skill(args: str, ctx: AgentCtx) -> None:
    loader = _resolve_loader(ctx)
    if loader is None:
        print("  [skill] no skill loader attached (skills/ not mounted)")
        return

    parts = split_args(args)
    sub = parts[0] if parts else "list"

    if sub == "list":
        _list(loader)
    elif sub == "view":
        if len(parts) < 2:
            print("  [skill] usage: /skill view <name>")
            return
        _view(loader, parts[1], getattr(ctx, "current_session_id", None))
    elif sub == "reload":
        _reload(loader)
    else:
        print(f"  [skill] unknown sub-command: {sub} (try list / view <name> / reload)")


def _list(loader) -> None:
    metas = loader.list_metadata()
    if not metas:
        print("  [skill] (none — skills/ empty or all filtered by platform)")
        return
    print(f"  [skill] {len(metas)} loaded:")
    for m in metas:
        # description 截断 80 字符与 /memory 风格一致
        desc = m.description
        if len(desc) > 80:
            desc = desc[:80] + "..."
        # V26.1：env 缺失的 skill 标 ⚠ setup（软标记；requires_tools 硬隐藏由
        # PromptBuilder 索引层处理，/skill list 故意全展示便于人工排查门控）
        missing = getattr(m, "missing_env_vars", lambda: [])()
        suffix = "  ⚠ (setup: set " + ", ".join(missing) + ")" if missing else ""
        print(f"    - {m.name}: {desc}{suffix}")


def _view(loader, name: str, session_id: str | None = None) -> None:
    try:
        content = loader.view(name, session_id)  # V26.2: ${SESSION_ID} 替换
    except KeyError:
        avail = ", ".join(loader.names()) or "(none)"
        print(f"  [skill] unknown: {name}. available: {avail}")
        return
    except FileNotFoundError as exc:
        print(f"  [skill] file vanished: {exc}")
        return
    print(content)

    # 末尾列出 bundled 资源（tier 3），人工对照 agent 看到的 linked_files
    try:
        resources = loader.list_resources(name)
    except (AttributeError, KeyError):
        resources = {}
    if resources:
        print("\n  [skill] linked files (tier 3):")
        for category, files in resources.items():
            for rel in files:
                print(f"    - {category}: {rel}")
        print("  [skill] read one with: skill_view(name, '<path>')")


def _reload(loader) -> None:
    before = len(loader)
    loader.scan()
    after = len(loader)
    delta = after - before
    sign = "+" if delta >= 0 else ""
    print(f"  [skill] reloaded: {before} → {after} ({sign}{delta})")
