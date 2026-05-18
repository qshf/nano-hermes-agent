"""
ToolRegistry — 工具注册表。

V3：新增 _generation 计数器 + check_fn TTL 缓存。
- _generation：每次 register/deregister 递增，供外层缓存判断是否失效
- check_fn 结果缓存 30s，避免每轮都 fork 进程探测
"""

import json
import logging
import time
from typing import Callable, Optional

CHECK_FN_TTL = 30.0  # check_fn 缓存有效期（秒）

log = logging.getLogger(__name__)


class ToolRegistry:
    """全局工具注册表：存储 schema + handler + check_fn，带缓存。"""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._generation: int = 0
        # 内层缓存：check_fn 结果 → (timestamp, bool)，30s TTL
        self._check_fn_cache: dict[str, tuple[float, bool]] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def register(
        self,
        schema: dict,
        handler: Callable[[dict], str],
        check_fn: Optional[Callable[[], bool]] = None,
    ):
        """注册一个工具。每次注册递增 generation。"""
        name = schema["name"]
        self._tools[name] = {
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
        }
        self._generation += 1

    def deregister(self, name: str):
        """移除一个工具。"""
        if name in self._tools:
            del self._tools[name]
            self._check_fn_cache.pop(name, None)
            self._generation += 1

    def _is_available(self, name: str, entry: dict) -> bool:
        """判断工具是否可用，check_fn 结果带 TTL 缓存。"""
        check_fn = entry["check_fn"]
        if check_fn is None:
            return True

        now = time.time()
        cached = self._check_fn_cache.get(name)
        if cached and (now - cached[0]) < CHECK_FN_TTL:
            log.debug("[内层缓存] 命中 tool=%s, result=%s, ttl_remaining=%.1fs", name, cached[1], CHECK_FN_TTL - (now - cached[0]))
            return cached[1]

        result = check_fn()
        self._check_fn_cache[name] = (now, result)
        log.debug("[内层缓存] 未命中 tool=%s, check_fn() → %s", name, result)
        return result

    def get_definitions(self, names: list[str]) -> list[dict]:
        """按名称列表返回可用工具的 OpenAI schema。check_fn 带 TTL 缓存。"""
        result = []
        for name in sorted(names):
            entry = self._tools.get(name)
            if entry is None:
                continue
            if not self._is_available(name, entry):
                continue
            result.append({"type": "function", "function": entry["schema"]})
        return result

    def get_openai_tools(self) -> list[dict]:
        """返回所有可用工具（兼容旧用法）。"""
        return self.get_definitions(list(self._tools.keys()))

    def dispatch(self, name: str, args: dict) -> str:
        """根据工具名分发调用。"""
        entry = self._tools.get(name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
        return entry["handler"](args)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def available_tool_names(self) -> list[str]:
        """只返回 check_fn 通过的工具名（带 TTL 缓存）。"""
        return [
            name for name, entry in self._tools.items()
            if self._is_available(name, entry)
        ]


# 全局单例
registry = ToolRegistry()
