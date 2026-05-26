"""V21.1: slash command registry + dispatcher.

设计原则
========
1. **装饰器自动注册**：每个命令在自己的文件里 ``@command("/name", description=...)``
   声明，``cli/commands/__init__.py`` 启动期 import 时副作用注册。
2. **dispatch 与命令解耦**：主循环只看见 ``dispatch(line, ctx) -> bool``，
   返回 True 表示已处理（主循环 ``continue``），返回 False 表示非 slash。
3. **handler 签名固定**：``def handler(args: str, ctx: AgentCtx) -> None``。
   全部运行期对象通过 ``ctx`` 传入，避免 5-8 参冗长签名。
4. **alias 通过双键写入**：alias 命中等价于主名命中，无特殊代码路径。

与源项目的差异
============
源项目 ``hermes_cli/commands.py`` 用中央 ``COMMAND_REGISTRY: list[CommandDef]``
数据驱动 + 多端点（CLI / gateway / Telegram / Slack / autocomplete）派生。
nano 命令规模 ≤ 20 个、单端点、教学优先，用装饰器 + 文件级隔离更轻；新增命令
只需新增一个文件，无需改中央数据表。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Iterable

from cli.context import AgentCtx

CommandHandler = Callable[[str, AgentCtx], None]


@dataclass(frozen=True)
class CommandDef:
    name: str                          # canonical name without slash, e.g. "memory"
    description: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    args_hint: str = ""                # for /help — e.g. "<list|view|reload>"
    category: str = "general"          # for /help 分组


# ── 全局注册表 ─────────────────────────────────────────────────────────────
# key 同时存主名和 alias，命中等价
_REGISTRY: dict[str, CommandDef] = {}


def command(
    name: str,
    *,
    description: str,
    aliases: Iterable[str] = (),
    args_hint: str = "",
    category: str = "general",
) -> Callable[[CommandHandler], CommandHandler]:
    """装饰器：注册一个 slash 命令。

    ``name`` 可带或不带前导 ``/``，会被规范化（统一去掉）。同样适用于 aliases。
    """

    canonical = name.lstrip("/")
    norm_aliases = tuple(a.lstrip("/") for a in aliases)

    def decorator(fn: CommandHandler) -> CommandHandler:
        cmd = CommandDef(
            name=canonical,
            description=description,
            handler=fn,
            aliases=norm_aliases,
            args_hint=args_hint,
            category=category,
        )
        _REGISTRY[canonical] = cmd
        for alias in norm_aliases:
            _REGISTRY[alias] = cmd
        return fn

    return decorator


def registered_commands() -> list[CommandDef]:
    """返回去重后的命令列表（aliases 不会重复出现）。

    用于 ``/help`` 渲染。按 (category, name) 排序。
    """

    seen: set[str] = set()
    result: list[CommandDef] = []
    for cmd in _REGISTRY.values():
        if cmd.name in seen:
            continue
        seen.add(cmd.name)
        result.append(cmd)
    result.sort(key=lambda c: (c.category, c.name))
    return result


# ── dispatch ───────────────────────────────────────────────────────────────


def dispatch(line: str, ctx: AgentCtx) -> bool:
    """分词并路由。返回 True 表示已处理（主循环应 continue）。

    - 非 ``/`` 开头 → 返回 False（主循环按 LLM 输入处理）
    - 命中已注册命令 → 调 handler，handler 异常被捕获并打印
    - 未命中 → 打印 ``unknown command`` + ``/help`` 提示，仍返回 True（被处理）
    """

    if not line.startswith("/"):
        return False

    body = line[1:].strip()
    if not body:
        # 单个 "/" 视为请求帮助
        body = "help"

    # split once: 命令名 + 余下 args 字符串
    parts = body.split(maxsplit=1)
    name = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    cmd = _REGISTRY.get(name)
    if cmd is None:
        print(f"  [error] unknown command: /{name} (try /help)")
        return True

    try:
        cmd.handler(args, ctx)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"  [error] /{name}: {exc!r}")
    return True


# ── 工具函数：handler 内部解析 args ─────────────────────────────────────────


def split_args(args: str) -> list[str]:
    """空白分词，支持引号包裹（仿 shlex.split）。

    handler 内部需要拆 sub-command / 路径时用。args 为空时返回空列表。
    """

    args = args.strip()
    if not args:
        return []
    try:
        return shlex.split(args)
    except ValueError:
        # 不平衡引号兜底为简单 split
        return args.split()
