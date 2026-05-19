"""
BuiltinMemoryProvider — 将 V6 的 MemoryStore 包装为 Provider 实现。

职责分离：
- MemoryStore: 纯存储引擎（文件 I/O、§ 分隔、字符限制）
- BuiltinMemoryProvider: 工具接口 + 生命周期契约

对应源项目：tools/memory_tool.py 中的 builtin 逻辑。
"""

import json
from typing import Any

from memory.provider import MemoryProvider
from tools.memory_store import MemoryStore

MEMORY_GUIDANCE = """Use the `memory` tool to persist important information across sessions:
- User preferences and corrections
- Project context and conventions
- Facts you've learned that will be useful later
Do NOT store: task progress, session-specific state, or information already in files."""

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Manage your persistent memory. Use this to remember important information "
        "across sessions: user preferences, project context, corrections, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "Action to perform on memory.",
            },
            "content": {
                "type": "string",
                "description": "The memory content to add, or the new content for replace.",
            },
            "old_text": {
                "type": "string",
                "description": "Substring to locate the target entry (for replace/remove).",
            },
        },
        "required": ["action"],
    },
}


class BuiltinMemoryProvider(MemoryProvider):
    """内置文件记忆 Provider。包装 MemoryStore 实现 ABC。"""

    def __init__(self):
        self._store = MemoryStore()

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        self._store.load()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [MEMORY_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        action = args.get("action")
        content = args.get("content", "")
        old_text = args.get("old_text", "")

        if action == "add":
            result = self._store.add(content)
        elif action == "replace":
            result = self._store.replace(old_text, content)
        elif action == "remove":
            result = self._store.remove(old_text)
        else:
            result = {"error": f"Unknown action: {action}"}

        return json.dumps(result, ensure_ascii=False)

    def system_prompt_block(self) -> str:
        block = self._store.snapshot or ""
        if block:
            block = block + "\n\n" + MEMORY_GUIDANCE
        return block

    def shutdown(self) -> None:
        pass

    @property
    def store(self) -> MemoryStore:
        """暴露底层 store（用于 /memory 命令等直接访问场景）。"""
        return self._store
