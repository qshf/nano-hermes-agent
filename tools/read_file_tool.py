"""
Read File Tool — 读取文件内容。

V1：通过 registry.register() 自注册。
V21.4：用 tool_result/tool_error 替换重复 json.dumps；字段名 ``content`` 与
       源项目 hermes-agent ``read_file`` 保持一致（用于"文件类内容"语义）。
"""

from pathlib import Path

from tools.registry import registry
from tools.result import tool_error, tool_result

READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": (
        "Read the contents of a file and return it with line numbers. "
        "Use this to inspect source code, config files, logs, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
        },
        "required": ["path"],
    },
}


def read_file_handler(args: dict) -> str:
    path_str = args.get("path", "")
    if not path_str:
        return tool_error("Path is required.")

    path = Path(path_str).expanduser()
    if not path.exists():
        return tool_error(f"File not found: {path}")
    if not path.is_file():
        return tool_error(f"Not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return tool_result(content=numbered)
    except Exception as exc:
        return tool_error(str(exc))


# 自注册
registry.register(READ_FILE_SCHEMA, read_file_handler)
