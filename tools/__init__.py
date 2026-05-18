"""
工具包初始化 — 自动发现并导入所有工具模块。

V1 核心机制：import tools 时，自动扫描 tools/ 下所有 *_tool.py，
触发每个工具的自注册逻辑。

V3 新增：load_plugin() 支持运行时动态加载 plugins/ 下的工具。
"""

import importlib
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent
_PLUGINS_DIR = _TOOLS_DIR.parent / "plugins"


def discover_tools():
    """扫描 tools/ 目录，import 所有 *_tool.py 模块以触发自注册。"""
    for file in sorted(_TOOLS_DIR.glob("*_tool.py")):
        module_name = f"tools.{file.stem}"
        importlib.import_module(module_name)


def load_plugin(filename: str):
    """运行时动态加载 plugins/ 下的工具文件，触发注册 + generation 递增。"""
    path = _PLUGINS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Plugin not found: {path}")
    module_name = f"plugins.{path.stem}"
    if module_name in sys.modules:
        return  # 已加载
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


discover_tools()
