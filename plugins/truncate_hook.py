"""
Truncate Hook — 截断过长的工具结果。

演示 transform_tool_result 钩子：当结果超过 MAX_CHARS 时截断。
返回 None 表示不修改，返回字符串则替换原结果。
"""

MAX_CHARS = 2000


def transform_tool_result(tool_name, args, result, **kw):
    if len(result) > MAX_CHARS:
        return result[:MAX_CHARS] + "\n...[truncated]"
    return None


def register(hook_manager):
    hook_manager.register("transform_tool_result", transform_tool_result)


def deregister(hook_manager):
    hook_manager.deregister("transform_tool_result", transform_tool_result)
