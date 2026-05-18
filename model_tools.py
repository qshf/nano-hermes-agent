"""
model_tools — Agent 获取工具定义的入口。

V3：新增缓存层。cache_key = (enabled_toolsets, registry._generation)。
注册表没变时直接返回缓存结果，避免每轮重算。
"""

import tools  # noqa: F401 — 触发自动发现
from tools.registry import registry
from toolsets import resolve_toolsets

# 外层缓存：cache_key = (enabled_toolsets, generation)，generation 不变则直接返回
_cache_key: tuple | None = None
_cache_definitions: list[dict] = []
_cache_available: list[str] = []


def _make_cache_key(enabled_toolsets: list[str]) -> tuple:
    return (tuple(sorted(enabled_toolsets)), registry.generation)


def get_tool_definitions(enabled_toolsets: list[str]) -> list[dict]:
    """根据启用的 toolset 列表，返回可用工具的 OpenAI schema。带缓存。"""
    global _cache_key, _cache_definitions

    key = _make_cache_key(enabled_toolsets)
    # 外层缓存命中：generation 没变，直接返回
    if key == _cache_key:
        return _cache_definitions

    # 外层缓存未命中：重新解析 toolset → 进入 registry 内层（check_fn TTL 缓存）
    tool_names = resolve_toolsets(enabled_toolsets)
    # 包含 MCP 动态注册的工具（mcp_ 前缀）
    mcp_tools = [n for n in registry.tool_names if n.startswith("mcp_")]
    all_names = sorted(set(tool_names + mcp_tools))
    _cache_definitions = registry.get_definitions(all_names)
    _cache_key = key
    return _cache_definitions


def get_available_tool_names(enabled_toolsets: list[str]) -> list[str]:
    """返回当前可用的工具名（经过 check_fn 过滤）。带缓存。"""
    global _cache_key, _cache_available

    key = _make_cache_key(enabled_toolsets)
    if key == _cache_key and _cache_available:
        return _cache_available

    tool_names = resolve_toolsets(enabled_toolsets)
    mcp_tools = [n for n in registry.tool_names if n.startswith("mcp_")]
    all_names = sorted(set(tool_names + mcp_tools))
    available = set(registry.available_tool_names)
    _cache_available = [name for name in all_names if name in available]
    return _cache_available


def invalidate_cache():
    """手动失效缓存（测试用）。"""
    global _cache_key, _cache_definitions, _cache_available
    _cache_key = None
    _cache_definitions = []
    _cache_available = []
