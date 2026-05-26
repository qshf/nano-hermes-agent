"""V21.1: slash 命令注册表 + dispatch。

主循环原本 ~205 行 if-elif 链已下沉到 ``cli/commands/<name>.py``，
``main.py`` 只看到 ``cli.dispatch(line, ctx)`` 这一个调用入口。

每加一个新命令：在 ``cli/commands/`` 下新建一个 ``.py``，用 ``@command(...)``
装饰器注册即可，主循环零改动。``cli/commands/__init__.py`` 启动期 import
所有命令模块触发装饰器注册。
"""

from cli.registry import CommandDef, command, dispatch, registered_commands
from cli.context import AgentCtx

# 启动期触发所有命令模块的装饰器注册
import cli.commands  # noqa: F401  — 副作用 import：触发命令注册

__all__ = [
    "CommandDef",
    "AgentCtx",
    "command",
    "dispatch",
    "registered_commands",
]
