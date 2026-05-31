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
- V22（已落地）: 加 ``cancel_token`` —— 由 main 启动 SIGINT handler 时挂载，
  slash handler 只读不动它。
- V23.3（已落地）: 把 ``stream_enabled`` / ``cancel_token`` 统一收到
  ``runtime: AgentRuntime`` 里，让父子共享同一份可变状态。``ctx.stream_enabled``
  / ``ctx.cancel_token`` 退化为 ``runtime.*`` 的直读 / 直写代理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.runtime import AgentRuntime


@dataclass
class AgentCtx:
    """slash handler 共享的运行期上下文。

    可变字段（handler 可直接 mutate）：
    - ``messages``        当前对话历史；``/new`` / ``/resume`` 会原地清空重建
    - ``current_session_id``  当前会话 ID；``/new`` / ``/resume`` 切换
    - ``turn_count``      轮次计数；``/new`` / ``/resume`` 重置为 0
    - ``stream_enabled``  代理 ``runtime.stream_enabled``；``/stream on|off`` 直写

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
    - ``runtime``         V23.3 起：父子共享的 ``AgentRuntime``，``stream_enabled``
                          / ``cancel_token`` 的唯一存储
    - ``cancel_token``    代理 ``runtime.cancel_token``（只读）
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
    runtime: AgentRuntime              # 父子共享的可变运行期状态（V23.3+）
    prompt_builder: Optional[Any] = None   # agent.prompt_builder.PromptBuilder（V21.2+）
    skill_loader: Optional[Any] = None     # agent.skill_loader.SkillLoader（V21.3+）
    session_store: Optional[Any] = None    # agent.session_store.SessionStore（V24.0+）

    # 后续版本扩展位
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def stream_enabled(self) -> bool:
        return bool(self.runtime.stream_enabled)

    @stream_enabled.setter
    def stream_enabled(self, value: bool) -> None:
        self.runtime.stream_enabled = bool(value)

    @property
    def cancel_token(self) -> Any:
        return self.runtime.cancel_token
