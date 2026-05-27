"""V21.1: AgentCtx — slash handler 共享的运行期上下文。

设计原则
========
- 把 main.py 主循环里所有 handler 用得上的运行期对象打包成一个 dataclass，
  避免 ``handler(args, messages, chain, manager, compressor, registry, ...)``
  这种 5-8 参冗长签名。
- 可变字段（messages / current_session_id / turn_count）用 mutable dataclass
  + 字段级语义文档，让 handler 知道哪些可以改。``/new`` / ``/resume`` 等
  生命周期类命令需要直接 mutate ``messages`` 和 ``current_session_id``。

后续版本扩展
============
- V21.2（已落地）: ``prompt_builder: PromptBuilder``，``/new`` / ``/resume``
  直接调 ``ctx.prompt_builder.build()`` 重建 system prompt。``build_system_prompt``
  字段保留为兼容回调（仍是 ``prompt_builder.build`` 的 thin wrapper），
  V21.3 再根据需要清理。
- V21.3（已落地）: 加 ``skill_loader: SkillLoader``，``/skill`` 命令通过它
  list / view / reload；``PromptBuilder`` 也读它生成 tier 1 索引段。
- V22+: 加 ``cancel_token`` / ``stream_state`` 等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class AgentCtx:
    """slash handler 共享的运行期上下文。

    可变字段（handler 可直接 mutate）：
    - ``messages``        当前对话历史；``/new`` / ``/resume`` 会原地清空重建
    - ``current_session_id``  当前会话 ID；``/new`` / ``/resume`` 切换
    - ``turn_count``      轮次计数；``/new`` / ``/resume`` 重置为 0

    引用字段（handler 只读取，不 mutate）：
    - ``chain``           V19 TransportChain
    - ``client``          chain.primary_client，仅 compressor 调用兼容用
    - ``model``           默认 model（chain entries 不内联时回退到此）
    - ``memory_manager``  V8 MemoryManager
    - ``builtin_provider`` 内置 builtin memory provider（``/memory`` 用）
    - ``compressor``      V15 ContextCompressor
    - ``registry``        ToolRegistry
    - ``enabled_toolsets`` 当前启用的 toolset 列表
    - ``prompt_builder``  V21.2 起：三段式 PromptBuilder。``/new`` / ``/resume``
                          通过 ``ctx.prompt_builder.build()`` 重建 system prompt
    - ``build_system_prompt``  V21.1 兼容回调；V21.2 起默认指向
                               ``prompt_builder.build``，handler 可继续调
    """

    # 可变 — handler 可改
    messages: list[dict]
    current_session_id: str
    turn_count: int

    # 引用 — handler 只读
    chain: Any                         # transports.chain.TransportChain
    client: Any                        # chain.primary_client
    model: str
    memory_manager: Any                # memory.MemoryManager
    builtin_provider: Any              # memory.BuiltinMemoryProvider | None
    compressor: Any                    # context_compressor.ContextCompressor
    registry: Any                      # tools.registry.ToolRegistry
    enabled_toolsets: list[str]
    build_system_prompt: Callable[[], str]
    prompt_builder: Optional[Any] = None   # agent.prompt_builder.PromptBuilder（V21.2+）
    skill_loader: Optional[Any] = None     # agent.skill_loader.SkillLoader（V21.3+）

    # 后续版本扩展位（V22 cancel_token / stream_state 等）
    extras: dict[str, Any] = field(default_factory=dict)

