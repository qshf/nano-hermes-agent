"""
Rate Limit Hook — 简单限流。

演示 pre_tool_call 的阻止功能：每分钟最多 MAX_CALLS_PER_MINUTE 次调用。
超限时返回 {"action": "block", "message": "..."} 阻止工具执行。
"""

import time

_call_times: list[float] = []
MAX_CALLS_PER_MINUTE = 5


def pre_tool_call(tool_name, args, **kw):
    now = time.time()
    # 清理超过 60s 的记录
    _call_times[:] = [t for t in _call_times if now - t < 60]
    if len(_call_times) >= MAX_CALLS_PER_MINUTE:
        return {"action": "block", "message": f"Rate limit: max {MAX_CALLS_PER_MINUTE} calls/min"}
    _call_times.append(now)
    return None


def register(hook_manager):
    hook_manager.register("pre_tool_call", pre_tool_call)


def deregister(hook_manager):
    hook_manager.deregister("pre_tool_call", pre_tool_call)
    _call_times.clear()
