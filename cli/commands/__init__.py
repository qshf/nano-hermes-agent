"""启动期触发所有命令模块的装饰器注册。

新增命令时：在本目录新建 ``<name>.py``，并在下面加一行 import 即可。
"""

# noqa: F401 — 副作用 import：触发 @command 装饰器把命令写进 _REGISTRY
from cli.commands import (  # noqa: F401
    help as _help_cmd,
    memory,
    load,
    mcp,
    plugin,
    tools,
    session,
    compress,
    transport,
    skill,
)
