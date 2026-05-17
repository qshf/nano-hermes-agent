"""
工具包初始化 — 自动发现并导入所有工具模块。

V1 核心机制：import tools 时，自动扫描 tools/ 下所有 *_tool.py，
触发每个工具的自注册逻辑。
"""

import importlib
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent


def discover_tools():
    """扫描 tools/ 目录，import 所有 *_tool.py 模块以触发自注册。"""
    for file in sorted(_TOOLS_DIR.glob("*_tool.py")):
        module_name = f"tools.{file.stem}"
        importlib.import_module(module_name)


discover_tools()
