"""
Terminal Tool — 执行 shell 命令并返回结果。

V1：通过 registry.register() 自注册，不再需要 agent.py 手动 import。
V21.4：用 tool_result/tool_error 收口返回格式。
"""

import subprocess

from tools.registry import registry
from tools.result import tool_error, tool_result

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": (
        "Execute a shell command on the local machine and return its output. "
        "Use this for file operations, running scripts, installing packages, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait. Defaults to 30.",
            },
        },
        "required": ["command"],
    },
}


def terminal_handler(args: dict) -> str:
    command = args.get("command", "")
    timeout = args.get("timeout", 30)

    if not command.strip():
        return tool_error("Command is required.")

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.returncode != 0:
            output += f"\n[stderr]\n{result.stderr}" if result.stderr else ""
            output += f"\n[exit code: {result.returncode}]"
        return tool_result(output=output.strip())
    except subprocess.TimeoutExpired:
        return tool_error(f"Command timed out after {timeout}s.")
    except Exception as exc:
        return tool_error(str(exc))


# 自注册
registry.register(TERMINAL_SCHEMA, terminal_handler)
