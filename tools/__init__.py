"""
工具包初始化 — 自动发现并导入所有工具模块。

V1 核心机制：import tools 时，自动扫描 tools/ 下所有 *_tool.py，
触发每个工具的自注册逻辑。

V3 新增：load_plugin() 支持运行时动态加载 plugins/ 下的工具。
V5 新增：插件生命周期管理（load/unload），支持 register/deregister 钩子。
"""

import importlib
import importlib.util
import sys
from pathlib import Path

from tools.hooks import hook_manager

_TOOLS_DIR = Path(__file__).parent
_PLUGINS_DIR = _TOOLS_DIR.parent / "plugins"

# 跟踪已加载的插件模块
_loaded_plugins: dict[str, object] = {}


def discover_tools():
    """扫描 tools/ 目录，import 所有 *_tool.py 模块以触发自注册。"""
    for file in sorted(_TOOLS_DIR.glob("*_tool.py")):
        module_name = f"tools.{file.stem}"
        importlib.import_module(module_name)


def load_plugin(filename: str):
    """加载插件：导入模块 + 调用 register(hook_manager) 注册钩子。"""
    path = _PLUGINS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Plugin not found: {path}")

    plugin_name = path.stem
    module_name = f"plugins.{plugin_name}"

    if plugin_name in _loaded_plugins:
        return

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # 如果插件导出 register()，调用它注册钩子
    if hasattr(module, "register"):
        module.register(hook_manager)

    _loaded_plugins[plugin_name] = module


def unload_plugin(filename: str) -> bool:
    """卸载插件：调用 deregister(hook_manager) 注销钩子，清理模块。"""
    plugin_name = Path(filename).stem
    module = _loaded_plugins.pop(plugin_name, None)
    if module is None:
        return False

    # 如果插件导出 deregister()，调用它注销钩子
    if hasattr(module, "deregister"):
        module.deregister(hook_manager)

    module_name = f"plugins.{plugin_name}"
    sys.modules.pop(module_name, None)
    return True


def list_plugins() -> dict[str, list[str]]:
    """返回已加载插件及其注册的钩子名称。"""
    result = {}
    for name, module in _loaded_plugins.items():
        hooks = []
        if hasattr(module, "_HOOKS"):
            hooks = module._HOOKS
        elif hasattr(module, "register"):
            # 从模块的函数名推断钩子
            from tools.hooks import VALID_HOOKS
            hooks = [h for h in VALID_HOOKS if hasattr(module, h)]
        result[name] = hooks
    return result


discover_tools()
