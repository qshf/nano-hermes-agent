"""
MemoryManager — 记忆 Provider 编排器（V8）。

V8 解决的问题：V7 的 agent.py 直接调用单个 provider。要支持第二个 provider，
就必须复制"调用 → 捕获异常 → 合并结果"的逻辑；没有工具路由（多个 provider
都暴露 tool 时谁来分发？）。

核心设计：
1. **单一集成点**：agent.py 只与 manager 对话，不感知 provider 数量。
2. **工具路由**：`_tool_to_provider` 字典按 tool 名找到目标 provider。
3. **错误隔离**：每个 provider 调用包裹 try/except，单个失败不影响其他。
4. **一个外部 provider 限制**：防止 tool schema 膨胀和后端冲突。

简化（相比源项目）：
- 无 prefetch_all / sync_all（V9 加生命周期时再加）
- 无 sanitize_context / build_memory_context_block 围栏辅助
  （V9 prefetch 把召回内容注入对话时才需要，到时再加）
- 无 on_session_end / on_pre_compress / on_memory_write 等高级钩子

对应源项目：agent/memory_manager.py
"""

from __future__ import annotations

import logging
from typing import Any

from memory.provider import MemoryProvider

logger = logging.getLogger(__name__)


# ─── Manager ─────────────────────────────────────────────────────────────────


class MemoryManager:
    """编排内置 provider 加最多一个外部 provider。

    内置 provider（name == "builtin"）始终被接受。第二个非内置 provider
    会被拒绝并打 warning — 这是为了防止 tool schema 膨胀和后端冲突。
    """

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._tool_to_provider: dict[str, MemoryProvider] = {}
        self._has_external: bool = False

    # -- 注册 -------------------------------------------------------------

    def add_provider(self, provider: MemoryProvider) -> None:
        """注册一个 provider。

        - 内置 provider（name == "builtin"）始终接受
        - 非内置 provider 只允许一个，第二个被拒绝
        - 同名 tool 冲突时保留先注册者
        """
        is_builtin = provider.name == "builtin"

        if not is_builtin:
            if self._has_external:
                existing = next(
                    (p.name for p in self._providers if p.name != "builtin"),
                    "unknown",
                )
                logger.warning(
                    "Rejected memory provider '%s' — external provider '%s' is "
                    "already registered. Only one external provider is allowed.",
                    provider.name,
                    existing,
                )
                return
            self._has_external = True

        self._providers.append(provider)

        # 工具名 → provider 索引（用于路由）
        for schema in provider.get_tool_schemas():
            tool_name = schema.get("name", "")
            if not tool_name:
                continue
            if tool_name in self._tool_to_provider:
                logger.warning(
                    "Memory tool name conflict: '%s' already registered by %s, "
                    "ignoring from %s",
                    tool_name,
                    self._tool_to_provider[tool_name].name,
                    provider.name,
                )
                continue
            self._tool_to_provider[tool_name] = provider

        logger.info(
            "Memory provider '%s' registered (%d tools)",
            provider.name,
            len(provider.get_tool_schemas()),
        )

    @property
    def providers(self) -> list[MemoryProvider]:
        """所有已注册的 provider（顺序：内置先于外部）。"""
        return list(self._providers)

    def get_provider(self, name: str) -> MemoryProvider | None:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    # -- system prompt ----------------------------------------------------

    def build_system_prompt(self) -> str:
        """收集所有 provider 的 system prompt block 并拼接。

        单个 provider 抛异常时打 warning 跳过，不阻塞其他 provider。
        """
        blocks = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' system_prompt_block() failed: %s",
                    provider.name,
                    e,
                )
        return "\n\n".join(blocks)

    # -- 工具 -------------------------------------------------------------

    def get_all_tool_schemas(self) -> list[dict[str, Any]]:
        """收集所有 provider 的 tool schema，按 name 去重。"""
        schemas = []
        seen = set()
        for provider in self._providers:
            try:
                for schema in provider.get_tool_schemas():
                    name = schema.get("name", "")
                    if name and name not in seen:
                        schemas.append(schema)
                        seen.add(name)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' get_tool_schemas() failed: %s",
                    provider.name,
                    e,
                )
        return schemas

    def get_all_tool_names(self) -> set[str]:
        return set(self._tool_to_provider.keys())

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """按 tool 名路由到对应 provider。

        provider 抛异常时返回 JSON 错误，不向上传播 — 让 agent 主循环
        能继续处理其他 tool call。
        """
        import json

        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return json.dumps(
                {"error": f"No memory provider handles tool '{tool_name}'"},
                ensure_ascii=False,
            )
        try:
            return provider.handle_tool_call(tool_name, args)
        except Exception as e:
            logger.warning(
                "Memory provider '%s' handle_tool_call(%s) failed: %s",
                provider.name,
                tool_name,
                e,
            )
            return json.dumps(
                {"error": f"Memory provider '{provider.name}' failed: {e}"},
                ensure_ascii=False,
            )

    # -- 生命周期 ---------------------------------------------------------

    def initialize_all(self, session_id: str = "", **kwargs) -> None:
        """初始化所有 provider。失败不阻塞其他 provider。"""
        for provider in self._providers:
            try:
                provider.initialize(session_id=session_id, **kwargs)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' initialize() failed: %s",
                    provider.name,
                    e,
                )

    def shutdown_all(self) -> None:
        """逆序关闭所有 provider。失败不阻塞其他 provider。"""
        for provider in reversed(self._providers):
            try:
                provider.shutdown()
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' shutdown() failed: %s",
                    provider.name,
                    e,
                )
