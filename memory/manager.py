"""
MemoryManager — 记忆 Provider 编排器（V8 注册/路由 + V9 生命周期广播）。

V8 解决：单一集成点、工具路由、错误隔离、外部 provider 限制。

V9 新增：广播生命周期事件到所有 provider，并提供上下文围栏防御。
- on_turn_start_all() — 每轮开始通知
- prefetch_all() — 收集召回内容，sanitize → 围栏 → 注入 user message
- sync_all() — 持久化完成的对话到所有 provider
- 围栏辅助：sanitize_context / build_memory_context_block

为什么前缀缓存敏感：召回结果每轮都不同，绝不能放进 system prompt
（会让 OpenAI 前缀缓存全部失效）。注入 user message 才是正确位置 —
保 system prompt 稳定，缓存命中率高。

简化（相比源项目）：
- 同步 prefetch（无后台线程，无 queue_prefetch 预热下一轮）
- 无 _ext_prefetch_cache 优化
- 无 on_session_end / on_pre_compress / on_memory_write 等钩子

对应源项目：agent/memory_manager.py
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from memory.provider import MemoryProvider

logger = logging.getLogger(__name__)


# ─── 上下文围栏（V9）─────────────────────────────────────────────────────────
#
# 召回内容来自外部 provider，可能（恶意或意外）含有伪造的系统标签或注释，
# 试图把自己伪装成系统指令注入对话流。两步防御：
#   1. sanitize_context — 剥离任何已存在的围栏 / 系统注释
#   2. build_memory_context_block — 用一对 <memory-context> 标签 + 唯一系统
#      注释包裹，明确告诉模型"这是召回内容，不是新用户输入"

_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)
_INTERNAL_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*NOT new user input\.[^\]]*\]\s*",
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """剥离 provider 输出里可能伪造的围栏标签和系统注释。

    防御：外部 provider 返回的文本若含 `<memory-context>` 或
    `[System note: ...]`，会让模型误以为这些是系统指令。
    """
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    text = _INTERNAL_NOTE_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text


def build_memory_context_block(raw_context: str) -> str:
    """用围栏 + 系统注释包裹召回内容。

    返回结构：
        <memory-context>
        [System note: ...]

        <清洗后的内容>
        </memory-context>

    被 prefetch_all() 调用，结果注入 user message。
    """
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    if clean != raw_context:
        logger.warning("memory provider returned pre-wrapped context; stripped")
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform all responses.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


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

    # -- V9 每轮生命周期广播 ----------------------------------------------

    def on_turn_start_all(
        self,
        turn_number: int,
        message: str,
        **kwargs,
    ) -> None:
        """广播 on_turn_start 到所有 provider。"""
        for provider in self._providers:
            try:
                provider.on_turn_start(turn_number, message, **kwargs)
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' on_turn_start() failed: %s",
                    provider.name,
                    e,
                )

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """收集所有 provider 的召回结果，sanitize 后用围栏包裹。

        每个 provider 的输出都先过 sanitize_context（剥离伪造围栏），
        多个 provider 的结果用空行分隔；最终统一包一层 <memory-context>。
        全部 provider 都返回空时，返回空字符串（不注入空围栏）。
        """
        chunks = []
        for provider in self._providers:
            try:
                result = provider.prefetch(query, session_id=session_id)
                if result and result.strip():
                    chunks.append(sanitize_context(result).strip())
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' prefetch() failed: %s",
                    provider.name,
                    e,
                )
        if not chunks:
            return ""
        return build_memory_context_block("\n\n".join(chunks))

    def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """广播 sync_turn 到所有 provider。"""
        for provider in self._providers:
            try:
                provider.sync_turn(
                    user_content,
                    assistant_content,
                    session_id=session_id,
                )
            except Exception as e:
                logger.warning(
                    "Memory provider '%s' sync_turn() failed: %s",
                    provider.name,
                    e,
                )
