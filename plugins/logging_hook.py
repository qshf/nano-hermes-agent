"""
Logging Hook — 打印每次工具调用的日志。

演示 pre_tool_call + post_tool_call 钩子的观察者用法。
加载后每次工具调用都会打印入口和出口日志（含耗时）。
"""


def pre_tool_call(tool_name, args, **kw):
    print(f"  [hook:log] → {tool_name}({args})")


def post_tool_call(tool_name, args, result, duration_ms, **kw):
    preview = result[:80] + "..." if len(result) > 80 else result
    print(f"  [hook:log] ← {tool_name} ({duration_ms}ms) {preview}")


def register(hook_manager):
    hook_manager.register("pre_tool_call", pre_tool_call)
    hook_manager.register("post_tool_call", post_tool_call)


def deregister(hook_manager):
    hook_manager.deregister("pre_tool_call", pre_tool_call)
    hook_manager.deregister("post_tool_call", post_tool_call)
