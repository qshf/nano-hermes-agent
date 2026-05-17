"""
Read File Tool — 读取文件内容。

V0 版本：最简实现，带行号输出。
"""

import json
from pathlib import Path

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
        return json.dumps({"error": "Path is required."}, ensure_ascii=False)

    path = Path(path_str).expanduser()
    if not path.exists():
        return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
    if not path.is_file():
        return json.dumps({"error": f"Not a file: {path}"}, ensure_ascii=False)

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return json.dumps({"content": numbered}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
