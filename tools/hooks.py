"""
HookManager — V5 钩子管理器。

管理 pre/post/transform 钩子的注册、注销和调用。
每个钩子回调用 try/except 隔离，一个插件出错不影响其他插件。

三种钩子：
- pre_tool_call(tool_name, args): 可阻止执行（返回 {"action": "block", "message": "..."}）
- post_tool_call(tool_name, args, result, duration_ms): 观察者，返回值忽略
- transform_tool_result(tool_name, args, result): 第一个非 None 字符串替换结果

使用方式：
    from tools.hooks import hook_manager
    hook_manager.register("pre_tool_call", my_callback)
    hook_manager.invoke("pre_tool_call", tool_name="terminal", args={...})
"""

import logging
from typing import Callable

log = logging.getLogger(__name__)

VALID_HOOKS = {"pre_tool_call", "post_tool_call", "transform_tool_result"}


class HookManager:
    """钩子管理器：注册、注销、调用回调。"""

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = {}

    def register(self, hook_name: str, callback: Callable):
        """注册一个钩子回调。"""
        if hook_name not in VALID_HOOKS:
            log.warning("Unknown hook: %s (valid: %s)", hook_name, VALID_HOOKS)
        self._hooks.setdefault(hook_name, []).append(callback)

    def deregister(self, hook_name: str, callback: Callable):
        """移除一个钩子回调。"""
        callbacks = self._hooks.get(hook_name, [])
        try:
            callbacks.remove(callback)
        except ValueError:
            pass

    def invoke(self, hook_name: str, **kwargs) -> list:
        """调用所有注册的回调，返回非 None 结果列表。

        每个回调独立 try/except，一个出错不影响其他。
        """
        callbacks = self._hooks.get(hook_name, [])
        results = []
        for cb in callbacks:
            try:
                ret = cb(**kwargs)
                if ret is not None:
                    results.append(ret)
            except Exception as e:
                log.warning("Hook '%s' callback %s raised: %s", hook_name, cb.__name__, e)
        return results

    @property
    def registered_hooks(self) -> dict[str, int]:
        """返回每个 hook 的回调数量。"""
        return {name: len(cbs) for name, cbs in self._hooks.items() if cbs}


hook_manager = HookManager()
