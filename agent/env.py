"""V27.1 — 统一环境变量解析（消除三处平行实现）。

为什么要它
==========
``_env_bool`` / ``_env_int`` / ``_env_float`` 原本在 ``turn_events.py`` 和
``voice_orchestrator_client.py`` 各抄了一份，``main.py`` 里还散落 6+ 处
``os.environ.get(...) not in ("0","false","False","")`` 的裸 idiom。语义本该
一致，但平行实现意味着"哪天把 'no'/'off' 也算 False"得改三个地方且容易漏。

布尔约定与历史对齐：``0`` / ``false`` / ``False`` / 空串视为 False，其余为 True。
数值非法值回退默认值，绝不抛——env 解析失败不该掀翻启动。
"""

from __future__ import annotations

import os

_FALSE_VALUES = ("0", "false", "False", "")


def env_bool(name: str, default: bool) -> bool:
    """读取布尔 env；``0/false/False/空串`` → False，未设置 → default。"""

    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw not in _FALSE_VALUES


def env_int(name: str, default: int) -> int:
    """读取整数 env，非法值回退 default。"""

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """读取浮点 env，非法值回退 default。"""

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
