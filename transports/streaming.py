"""V22 — 流式输出 + 中断的共享数据类型。

教学定位
--------
V17-V20 把 transport ABC 跑通了同步那一半（``call`` → 完整 ``NormalizedResponse``）。
V22 接着补**流式**这一半 — 同样的 transport 实例，按 ``stream_call`` 入口
吐出增量事件，agent loop 在第一个 token 到达后就能开始打印。

为什么不直接复用 ``call``
-----------------------
源项目 ``run_agent.py:7484`` 把流式做成 ``call`` 的内部分支（按
``stream_callback != None`` 切路径），代价是 800+ 行同一个方法里既要管
chunk 累积、又要管 finish_reason 推断、还要管 silent fallback。教学版选**正交**
路径：流式是独立方法，复用 ``build_kwargs`` / ``convert_messages``
/ ``convert_tools``，但产出形态完全不同（迭代器 vs 单值）。这样：

- ``call`` 仍然是"一行进、一行出"的同步路径 — V17-V21 的所有不变量测试不动
- ``stream_call`` 把"分片解析 + 累积重建"封装在 transport 内部，agent loop
  只需读 ``StreamEvent``
- agent loop 用 ``transport.stream_call`` 就拿到流式，``transport.call`` 就拿到
  非流式 — 两条路径明确，不必看 callback 是不是 None

事件模型
--------
``StreamEvent`` 是 transport 与 agent loop 之间的最小契约：

- ``text_delta``        — 文本增量（``text`` 字段）
- ``reasoning_delta``   — DeepSeek/Claude thinking 模式的 reasoning 增量
- ``tool_call_started`` — 第一次见到完整 tool name 时（``tool_name`` 字段）
- ``tool_arguments_delta`` — 工具参数流式增长元信息（不暴露参数正文）
- ``tool_arguments_finished`` — 工具参数增长结束元信息
- ``done``              — 流式正常结束，``response`` 字段给完整 ``NormalizedResponse``

``done`` 事件之前 transport 已经把所有增量重建成 ``NormalizedResponse`` —
agent loop 不需要自己拼 tool_calls / usage / finish_reason。

中断模型
--------
``CancelToken`` 是线程安全的取消标记 — main loop 在 SIGINT handler 里 ``cancel()``，
``stream_call`` 内部循环每帧 ``check()``，命中就抛 ``StreamCancelled`` 并 close
SDK 的 stream context。这与源项目 ``tools/interrupt.py`` 的 per-thread set
设计同构 — 但 nano 单 agent 单线程，不需要 thread-id 索引，``threading.Event``
即可。

对应源项目:
    ``run_agent.py:7484-7900`` _interruptible_streaming_api_call
    ``tools/interrupt.py`` 中断信号机制
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from transports.types import NormalizedResponse


# ── 事件类型 ─────────────────────────────────────────────────────────────


# 用 str 常量而不是 enum — agent loop 里 ``ev.type == "text_delta"``
# 比 ``ev.type is StreamEventType.TEXT_DELTA`` 更短。
EVENT_TEXT_DELTA = "text_delta"
EVENT_REASONING_DELTA = "reasoning_delta"
EVENT_TOOL_CALL_STARTED = "tool_call_started"
EVENT_TOOL_ARGUMENTS_DELTA = "tool_arguments_delta"
EVENT_TOOL_ARGUMENTS_FINISHED = "tool_arguments_finished"
EVENT_DONE = "done"


@dataclass
class StreamEvent:
    """transport 流式入口产出的统一增量事件。

    字段语义按 ``type`` 区分：
    - ``text_delta`` / ``reasoning_delta`` — ``text`` 字段是增量串
    - ``tool_call_started`` — ``tool_name`` 字段是工具名（只在第一次见到完整
      name 时 fire；后续 ``arguments`` 增量不暴露给 agent loop，在 transport
      内部累积重建到 ``NormalizedResponse.tool_calls``）
    - ``done`` — ``response`` 字段是完整 ``NormalizedResponse``（含 content /
      tool_calls / usage / finish_reason），等价于非流式 ``call`` 的返回值

    ``provider_data`` 是协议特定逃生口（同 ``NormalizedResponse``）。Anthropic
    的 thinking signature / Codex 的 response_item_id 等可塞进来。
    """

    type: str
    text: str = ""
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    argument_field: Optional[str] = None
    delta_chars: int = 0
    total_chars: int = 0
    response: Optional[NormalizedResponse] = None
    provider_data: Optional[dict[str, Any]] = field(default=None, repr=False)


# ── 中断信号 ─────────────────────────────────────────────────────────────


class StreamCancelled(RuntimeError):
    """流式被 ``CancelToken`` 中断 — 上层应回退到 prompt 而不是退出进程。

    与 ``KeyboardInterrupt`` 的区别：``KeyboardInterrupt`` 在 ``input()`` 等
    阻塞 syscall 上抛出（用户在 prompt 上按 Ctrl+C 就该退出 agent），
    ``StreamCancelled`` 是 agent loop 主动从 token 检查里抛出（流式中按
    Ctrl+C 应当只取消当前响应、回到 prompt）。
    """


class CancelToken:
    """线程安全的取消标记 — main loop 设置，``stream_call`` 检查。

    用 ``threading.Event`` 而不是裸 bool — V22 单线程也够用，但 V23 多 agent
    /  delegate 时父 token 会被多个子 agent 线程同时检查 / 设置，提前用
    Event 拿原子语义零成本。

    生命周期：
    1. main loop 启动一轮对话前 ``token.reset()``
    2. SIGINT handler 在流式期间 ``token.cancel()``
    3. ``stream_call`` 每帧 ``token.check()``，命中即 raise StreamCancelled
    4. 异常被 main loop catch，打印 "[cancelled]" 并回到 prompt

    ``check()`` 而不是 ``raise_if_cancelled`` — 调用频率高（每个 SSE chunk
    一次），简短名字让 hot path 视觉负担更低。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """标记取消 — 幂等，可被 SIGINT handler 多次调用。"""
        self._event.set()

    def reset(self) -> None:
        """清除取消标记 — main loop 启动新一轮前调用。"""
        self._event.clear()

    def is_cancelled(self) -> bool:
        """非阻塞查询 — agent loop 决策路径用，不会抛异常。"""
        return self._event.is_set()

    def check(self) -> None:
        """命中即 raise — ``stream_call`` 内部循环用，省去 if 分支。"""
        if self._event.is_set():
            raise StreamCancelled("Stream cancelled by user")


# ── 公共类型别名 ──────────────────────────────────────────────────────────


StreamIterator = Iterator[StreamEvent]
"""``stream_call`` 的返回类型 — agent loop 用 for 循环消费。

最后一个事件保证是 ``type == EVENT_DONE``（除非中途抛 ``StreamCancelled``）。
"""
