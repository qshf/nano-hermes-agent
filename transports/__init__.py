"""V17 — Transport 注册表 + 自动发现。

教学定位
--------
注册表的存在让"挂第二个家族"成为零接触新增 — V18 加 AnthropicTransport
时，只需在 ``anthropic.py`` 末尾 ``register_transport("anthropic_messages", ...)``
即可被 ``get_transport()`` 找到，agent loop 一行不动。

V17 阶段只有 ``chat_completions`` 一家，注册表看起来是大材小用 — 这是有意为之，
让 V18 的多 transport 故事在 V17 阶段已经把基础设施摆好。

对应源项目: ``hermes-agent/agent/transports/__init__.py``。
"""

from transports.types import (  # noqa: F401
    NormalizedResponse,
    ToolCall,
    Usage,
    build_tool_call,
)

_REGISTRY: dict = {}
_discovered: bool = False


def register_transport(api_mode: str, transport_cls: type) -> None:
    """注册一个 transport 类到指定 api_mode 字符串。"""
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str):
    """按 api_mode 拿 transport 实例，未注册返回 None。

    返回 None 而非 raise，方便调用方做 graceful fallback —
    本项目 V17 唯一的 mode 是 ``chat_completions``。
    """
    global _discovered
    if not _discovered:
        _discover_transports()
    cls = _REGISTRY.get(api_mode)
    if cls is None:
        # 注册表可能因模块导入顺序而部分填充，miss 时再触发一次发现
        _discover_transports()
        cls = _REGISTRY.get(api_mode)
    if cls is None:
        return None
    return cls()


def _discover_transports() -> None:
    """import 所有 transport 模块触发自动注册。"""
    global _discovered
    _discovered = True
    try:
        import transports.chat_completions  # noqa: F401
    except ImportError:
        pass
    try:
        import transports.anthropic  # noqa: F401
    except ImportError:
        pass
