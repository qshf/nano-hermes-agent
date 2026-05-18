"""
Write File Tool — 写入文件内容。

用于演示动态加载：初始启动时不在 tools/ 目录的自动发现范围内，
通过运行时手动 import 触发注册，验证 generation 缓存失效机制。

使用方式：
    在 agent 运行中执行：
    >>> from tools import load_plugin
    >>> load_plugin("tools.write_file_tool")
"""

import json
from pathlib import Path

from tools.registry import registry

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": (
        "Write content to a file. Creates the file if it doesn't exist, "
        "overwrites if it does. Use this to create or modify files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["path", "content"],
    },
}


def write_file_handler(args: dict) -> str:
    path_str = args.get("path", "")
    content = args.get("content", "")

    if not path_str:
        return json.dumps({"error": "Path is required."}, ensure_ascii=False)

    path = Path(path_str).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({"result": f"Written {len(content)} chars to {path}"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


# 自注册
registry.register(WRITE_FILE_SCHEMA, write_file_handler)
