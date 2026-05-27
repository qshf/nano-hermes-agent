"""
Docker Exec Tool — 在 Docker 容器中执行命令。

V2 演示：通过 check_fn 实现运行时可用性判断。
Docker 未安装时，该工具不会暴露给 LLM。
V21.4：用 tool_result/tool_error 收口返回格式。
"""

import shutil
import subprocess

from tools.registry import registry
from tools.result import tool_error, tool_result

DOCKER_EXEC_SCHEMA = {
    "name": "docker_exec",
    "description": (
        "Execute a command inside a running Docker container. "
        "Only available when Docker is installed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "container": {
                "type": "string",
                "description": "Container name or ID.",
            },
            "command": {
                "type": "string",
                "description": "The command to execute inside the container.",
            },
        },
        "required": ["container", "command"],
    },
}


def docker_available() -> bool:
    """检查 Docker 是否已安装且可用。"""
    return shutil.which("docker") is not None


def docker_exec_handler(args: dict) -> str:
    container = args.get("container", "")
    command = args.get("command", "")

    if not container or not command:
        return tool_error("container and command are required.")

    try:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.returncode != 0:
            output += f"\n[stderr]\n{result.stderr}" if result.stderr else ""
            output += f"\n[exit code: {result.returncode}]"
        return tool_result(output=output.strip())
    except subprocess.TimeoutExpired:
        return tool_error("Command timed out after 30s.")
    except Exception as exc:
        return tool_error(str(exc))


# 自注册，带 check_fn
registry.register(DOCKER_EXEC_SCHEMA, docker_exec_handler, check_fn=docker_available)
