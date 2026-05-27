"""
ToolRegistry — 工具注册表。

V3：新增 _generation 计数器 + check_fn TTL 缓存。
V4：新增 is_async 标记 + _run_async 桥接 + dispatch 前自动 coerce。
V5：dispatch 集成 pre/post/transform 钩子。

- _generation：每次 register/deregister 递增，供外层缓存判断是否失效
- check_fn 结果缓存 30s，避免每轮都 fork 进程探测
- is_async：标记 handler 是否为 async def，dispatch 自动桥接
- coerce：dispatch 前根据 schema 自动修正参数类型
- hooks：dispatch 流程中的三个扩展点（pre/post/transform）
"""

import asyncio
import concurrent.futures
import json
import logging
import time
from typing import Callable, Optional

CHECK_FN_TTL = 30.0  # check_fn 缓存有效期（秒）

log = logging.getLogger(__name__)


# --- V4: 异步桥接 ---

def _run_async(coro) -> str:
    """在 sync 上下文中运行 async 协程。

    策略：
    - 没有 running loop → asyncio.run()（最简单，CLI 场景）
    - 有 running loop → 开新线程跑 asyncio.run()（嵌入 async 框架时）
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=60)
    else:
        return asyncio.run(coro)


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
        is_async: bool = False,
    ):
        """注册一个工具。每次注册递增 generation。"""
        name = schema["name"]
        self._tools[name] = {
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "is_async": is_async,
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
        """根据工具名分发调用。

        V5: coerce → pre_hook → 执行 → post_hook → transform。
        V21.4: 加最终防线 — 任何工具返回非字符串、抛出异常逃逸出 handler 自身的
              try/except、或返回非合法 JSON 字符串时，dispatch 兜底成统一格式，
              确保 messages[]["content"] 永远是合法 JSON 字符串。
        """
        from tools.result import tool_error, tool_result

        entry = self._tools.get(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")

        # V4: 类型强制转换
        from tools.coerce import coerce_args
        coerced = coerce_args(entry["schema"], args)

        # V5: pre_tool_call 钩子（可阻止执行）
        from tools.hooks import hook_manager
        pre_results = hook_manager.invoke("pre_tool_call", tool_name=name, args=coerced)
        for r in pre_results:
            if isinstance(r, dict) and r.get("action") == "block":
                return tool_error(f"Blocked: {r.get('message', '')}")

        # 执行 handler（计时） — V21.4: 顶层 try 兜底未捕获异常
        start = time.monotonic()
        try:
            if entry.get("is_async"):
                result = _run_async(entry["handler"](coerced))
            else:
                result = entry["handler"](coerced)
        except Exception as exc:
            log.exception("Tool %s dispatch error: %s", name, exc)
            return tool_error(f"Tool execution failed: {type(exc).__name__}: {exc}")
        duration_ms = int((time.monotonic() - start) * 1000)

        # V5: post_tool_call 钩子（观察者，返回值忽略）
        hook_manager.invoke("post_tool_call", tool_name=name, args=coerced, result=result, duration_ms=duration_ms)

        # V5: transform_tool_result 钩子（第一个非 None 字符串替换结果）
        transform_results = hook_manager.invoke("transform_tool_result", tool_name=name, args=coerced, result=result)
        for r in transform_results:
            if isinstance(r, str):
                result = r
                break

        # V21.4 最终防线：保证返回值是合法 JSON 字符串
        # 三种异常情况：(1) handler 返回非 str；(2) 返回 str 但不是合法 JSON；
        # (3) 返回合法 JSON 但是 list/数字等非 object 顶层类型 — 也强转。
        if not isinstance(result, str):
            log.warning("Tool %s returned non-str (%s); coercing", name, type(result).__name__)
            return tool_result(output=str(result))
        try:
            parsed = json.loads(result)
            if not isinstance(parsed, dict):
                log.warning("Tool %s returned non-dict JSON; coercing", name)
                return tool_result(output=result)
        except json.JSONDecodeError:
            log.warning("Tool %s returned non-JSON string; coercing", name)
            return tool_result(output=result)

        return result

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
