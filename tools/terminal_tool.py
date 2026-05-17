"""
Terminal Tool — 执行 shell 命令并返回结果。

V1：通过 registry.register() 自注册，不再需要 agent.py 手动 import。
"""

import json
import subprocess

from tools.registry import registry

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
        return json.dumps({"error": "Command is required."}, ensure_ascii=False)

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
        return json.dumps({"output": output.strip()}, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Command timed out after {timeout}s."}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


# 自注册
registry.register(TERMINAL_SCHEMA, terminal_handler)
