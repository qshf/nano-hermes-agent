"""
MemoryStore — 文件持久化记忆存储引擎。

纯存储层：文件 I/O、§ 分隔、字符限制、子串匹配。
不包含工具 schema 或注册逻辑（那些在 BuiltinMemoryProvider 中）。

V7 重构：从 tools/memory_tool.py 拆出，成为独立存储引擎。
"""

from pathlib import Path
from typing import Optional

ENTRY_DELIMITER = "\n§\n"
DEFAULT_CHAR_LIMIT = 2200
MEMORY_FILE = Path.home() / ".hermes" / "nano_memory" / "MEMORY.md"


class MemoryStore:
    """文件持久化记忆存储。

    双状态设计：
    - _snapshot: 加载时冻结，用于 system prompt（保持前缀缓存稳定）
    - _entries: 实时状态，随 tool 调用变化，tool 响应反映此状态
    """

    def __init__(self, file_path: Path = MEMORY_FILE, char_limit: int = DEFAULT_CHAR_LIMIT):
        self._file_path = file_path
        self._char_limit = char_limit
        self._entries: list[str] = []
        self._snapshot: Optional[str] = None

    def load(self):
        """从磁盘加载条目，冻结快照。"""
        self._entries = self._read_file()
        self._snapshot = self._render_block()

    @property
    def snapshot(self) -> Optional[str]:
        """返回冻结快照（用于 system prompt 注入）。"""
        return self._snapshot

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def entries(self) -> list[str]:
        return self._entries

    @property
    def char_limit(self) -> int:
        return self._char_limit

    def char_count(self) -> int:
        return sum(len(e) for e in self._entries)

    def add(self, content: str) -> dict:
        """添加一条记忆。"""
        content = content.strip()
        if not content:
            return {"error": "Content cannot be empty"}

        if content in self._entries:
            return {"error": "Duplicate entry"}

        new_total = self.char_count() + len(content)
        if new_total > self._char_limit:
            return {
                "error": f"Exceeds limit: {new_total}/{self._char_limit} chars. "
                         f"Remove old entries first."
            }

        self._entries.append(content)
        self._save_file()
        return self._success("Added")

    def replace(self, old_text: str, new_content: str) -> dict:
        """替换包含 old_text 子串的条目。"""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text or not new_content:
            return {"error": "old_text and content cannot be empty"}

        idx = self._find_entry(old_text)
        if idx is None:
            return {"error": f"No entry contains: '{old_text[:50]}'"}

        size_diff = len(new_content) - len(self._entries[idx])
        if self.char_count() + size_diff > self._char_limit:
            return {"error": "Replacement would exceed char limit"}

        self._entries[idx] = new_content
        self._save_file()
        return self._success("Replaced")

    def remove(self, old_text: str) -> dict:
        """移除包含 old_text 子串的条目。"""
        old_text = old_text.strip()
        if not old_text:
            return {"error": "old_text cannot be empty"}

        idx = self._find_entry(old_text)
        if idx is None:
            return {"error": f"No entry contains: '{old_text[:50]}'"}

        self._entries.pop(idx)
        self._save_file()
        return self._success("Removed")

    # ─── 内部方法 ────────────────────────────────────────────────────────

    def _find_entry(self, substring: str) -> Optional[int]:
        for i, entry in enumerate(self._entries):
            if substring in entry:
                return i
        return None

    def _success(self, action: str) -> dict:
        return {
            "status": "ok",
            "action": action,
            "entries": len(self._entries),
            "usage": f"{self.char_count()}/{self._char_limit} chars",
        }

    def _render_block(self) -> Optional[str]:
        if not self._entries:
            return None
        lines = "\n".join(f"- {entry}" for entry in self._entries)
        usage = f"{self.char_count()}/{self._char_limit} chars"
        return f"## Your Memory ({usage})\n{lines}"

    def _read_file(self) -> list[str]:
        if not self._file_path.exists():
            return []
        text = self._file_path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        return [e.strip() for e in text.split("§") if e.strip()]

    def _save_file(self):
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        text = ENTRY_DELIMITER.join(self._entries)
        self._file_path.write_text(text, encoding="utf-8")
