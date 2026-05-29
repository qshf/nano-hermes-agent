"""V23.0 / V23.3 — 子 agent loop。

教学定位
========
父 agent loop 在 ``main.py:run_agent`` 里，是含 memory / compress / streaming UI
的全功能形态。V23.0 引入"父子隔离"概念时面临选择：

A. 把 main.py 的 agent loop 抽成 ``AIAgent`` 类，父子复用同一份代码
B. 写一个**裁剪版** child loop，只保留"调 LLM → 跑工具 → 拼 messages"的核心三步

源项目走 A（``run_agent.py:run_conversation`` 同时被父和子调用，1300+ 行），
因为生产环境父子的所有特性都要等价（流式、cache、failover、trajectory 等）。
nano 走 B —— 子的能力本就受限（黑名单 memory、不进 UI），裁剪版让"父子的差异
在哪里"在代码层就一目了然，而不是埋在 ``role == "leaf"`` 这种 if 分支里。

V23.3 演进（本档）
------------------
- 加入 ``cancel_token`` / ``stream_enabled`` / ``progress_callback`` 三个可选参数
- ``stream_enabled`` 且 chain 支持 ``stream_call`` 时走流式：每个 ``StreamEvent``
  调一次 ``progress_callback``（侧路给父 stderr 用）；done 帧 ``response`` 字段
  即等价 ``NormalizedResponse``，与同步路径下游处理统一
- ``StreamCancelled`` 在子层被翻译为 ``exit_reason="interrupted"`` —— **不**重抛
  到父，让 delegate handler 能继续聚合还在跑的兄弟子的结果
- 工具循环 / LLM 调用前都 check ``cancel_token``，让父 cancel 能尽早 kill 子

V23.4 加结构化结果 + tool_trace 时，本函数返回值从只含 summary 升级为含
``tokens`` / ``tool_trace`` / ``status`` 的完整 dict（已预留 ``exit_reason`` 字段）。

不做的事（vs 父 loop）
----------------------
- 不调 ``memory_manager.*`` —— 子默认拿不到 memory 工具（隔离原则）
- 不调 ``compressor.*`` —— 子任务短小，max_iterations=8 兜底，不需要压缩
- 不打印任何 UI —— stdout 静默；progress 仅经 callback 走 stderr 侧路
- 不做 prefetch / sync_turn / on_session_switch —— 子无会话生命周期

对应源项目
----------
- 子 loop 主体：``tools/delegate_tool.py:1305-1593`` 的 ``_run_single_child``
  → nano 版裁掉 ThreadPoolExecutor、心跳、ACP transport、诊断 dump、
    文件读写跟踪 ~6 块，只剩"调 LLM ↔ 跑工具"循环 + V23.3 流式中继 + cancel
- 子 system prompt：``tools/delegate_tool.py:564-637`` 的 ``_build_child_system_prompt``
- V23.3 cancel 桥接：``tools/delegate_tool.py:2104-2139`` 的中断检测
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from tools.registry import ToolRegistry
from tools.result import tool_error
from transports.chain import FailoverExhausted, TransportChain
from transports.streaming import EVENT_DONE, CancelToken, StreamCancelled, StreamEvent
from transports.types import build_assistant_history_msg

logger = logging.getLogger(__name__)


# 子 system prompt 模板 —— 与父三段式 PromptBuilder 完全独立。
#
# 关键差异：
#   1. 不含父的角色 / 风格指令（避免父 prompt 被复刻进子上下文）
#   2. 不含 skill 索引段（V21.3 tier 1）—— 子默认拿不到 skill_view，索引也无意义
#   3. 不含 memory 围栏 —— 子拿不到 memory 工具
#   4. 显式列出"你只能用这些工具"，让 LLM 提前放弃幻觉调用 delegate_task / memory_*
#   5. 显式要求"达成目标后给出 concise summary"，避免子在父等待时无限提问
_CHILD_SYSTEM_TEMPLATE = """You are a sub-agent dispatched by a parent agent to complete a focused task.

Operating rules:
- Focus exclusively on the GOAL below. Do not chat, ask follow-up questions, or expand scope.
- You have access to a limited toolset (listed below). Tools the parent uses (memory, delegation,
  user clarification) are intentionally not available to you.
- When the goal is achieved (or determined infeasible), respond with a concise plain-text
  summary — no preamble, no offering further help. The parent will read this as your final result.
- If you reach the iteration budget without completing, summarize partial progress.

Available tools: {tool_list}

GOAL:
{goal}{context_block}"""


def _build_child_system_prompt(
    goal: str,
    context: str,
    tool_names: list[str],
) -> str:
    """渲染子 system prompt — 单一模板，不分支。"""
    tool_list = ", ".join(tool_names) if tool_names else "(none)"
    context_block = f"\n\nCONTEXT:\n{context.strip()}" if context and context.strip() else ""
    return _CHILD_SYSTEM_TEMPLATE.format(
        tool_list=tool_list,
        goal=goal.strip(),
        context_block=context_block,
    )


def run_child_loop(
    *,
    goal: str,
    context: str,
    chain: TransportChain,
    model: str,
    registry: ToolRegistry,
    allowed_tool_names: set[str],
    max_iterations: int = 8,
    cancel_token: Optional[CancelToken] = None,            # V23.3
    stream_enabled: bool = False,                          # V23.3: 启用 chain.stream_call 路径
    progress_callback: Optional[Callable[[StreamEvent], None]] = None,  # V23.3
) -> dict:
    """跑一个隔离的子 agent loop，返回结果 dict。

    V23.0 返回值（最小集）::

        {
            "summary": str,         # 子最后一条 assistant 文本（或失败 / 截断说明）
            "exit_reason": str,     # "completed" | "max_iterations" | "error" | "interrupted"
            "iterations": int,      # 实际跑了多少轮 LLM 调用
        }

    V23.3 新增 ``"interrupted"`` 状态（父 cancel 桥接到子时翻译而来）。
    V23.4 起会扩展 ``tokens`` / ``tool_trace`` / ``status`` 字段，但
    ``summary`` / ``exit_reason`` 是稳定承诺。

    流式路径（V23.3）
    -----------------
    - ``stream_enabled=True`` + ``chain.stream_call`` 可用 → 走流式：每收到
      ``StreamEvent`` 就调一次 ``progress_callback``（侧路给父 stderr 用），
      ``done`` 帧的 ``response`` 字段就是等价的 ``NormalizedResponse``，
      与同步路径下游处理统一。
    - ``stream_enabled=False`` 或 chain 没有 ``stream_call`` → 走 V23.0 同步
      ``chain.call``。这条退化路径让 V23.0/V23.1 测试的 fake chain 仍然能跑
      （fake chain 没实现 ``stream_call``）。

    中断路径（V23.3）
    -----------------
    - 父子共享同一个 ``cancel_token`` —— ``set_delegate_context`` 阶段已经
      把父侧 token 透到这里。
    - 子内层每次调 LLM 前先 ``check()``，命中即抛 ``StreamCancelled``。
    - **不**重抛到父：`StreamCancelled` 在子层被 catch 后翻译为
      ``exit_reason="interrupted"``，让父 ``delegate_task`` handler 能继续
      聚合其他还在跑的兄弟子的结果（cancel 是"软取消"，不是"硬抛错"）。
    - 同步路径里也做 cancel 检查（每轮 LLM 前 + tool 执行前），让 V23.0
      不变更测试用例的同时仍能被父 cancel kill。

    隔离保证（V23.0 测试覆盖）
    --------------------------
    - 调用方传入的 ``messages`` 不被 mutate —— 子在内部新建 list
    - 子 system prompt 不含父的 skill 索引段（构造时就不注入）
    - 子工具白名单 = ``allowed_tool_names`` ∩ ``registry.available_tool_names``
    - 子不持有 ``memory_manager`` 引用
    """
    # 构建子工具集：白名单 ∩ 当前可用 —— 同时过滤掉 check_fn 失败的工具
    available = set(registry.available_tool_names)
    effective_tools = sorted(allowed_tool_names & available)

    # 子 messages 完全新建，不复用父的 list 引用 —— 父的 messages 不受影响
    system_prompt = _build_child_system_prompt(goal, context, effective_tools)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal.strip()},
    ]

    # 子 tools schema —— 直接复用 registry.get_definitions，但只传白名单
    tools_schema = registry.get_definitions(effective_tools)

    # V23.3: 流式路径只在两个条件都成立时启用 —— ``stream_enabled`` 且 chain 有
    # ``stream_call`` 方法。fake chain（V23.0/V23.1 测试用）默认没实现 stream_call，
    # 所以即便测试里漏传 stream_enabled=False 也能优雅退化。
    use_stream = bool(stream_enabled) and hasattr(chain, "stream_call")

    def _is_cancelled() -> bool:
        return cancel_token is not None and cancel_token.is_cancelled()

    iterations = 0
    last_text = ""

    while iterations < max_iterations:
        iterations += 1
        logger.debug(
            "[child_loop] iter=%d goal_prefix=%r stream=%s",
            iterations, goal[:60], use_stream,
        )

        # 每轮 LLM 前先 check cancel —— 让"父 cancel 时 worker 已经在跑下一轮"的
        # 场景能在 LLM 调用前就退出，省一次 token
        if _is_cancelled():
            return {
                "summary": "sub-agent cancelled before LLM call",
                "exit_reason": "interrupted",
                "iterations": iterations - 1,  # 这一轮没真跑
            }

        try:
            if use_stream:
                # 流式路径 —— 转发事件到 progress_callback，最后一个 done 帧
                # 的 ``response`` 就是等价 NormalizedResponse
                normalized = None
                for ev in chain.stream_call(
                    cancel_token=cancel_token,
                    model=model,
                    messages=messages,
                    tools=tools_schema,
                ):
                    if progress_callback is not None:
                        try:
                            progress_callback(ev)
                        except Exception:  # noqa: BLE001 — callback 不能炸 loop
                            logger.exception("[child_loop] progress_callback raised")
                    if ev.type == EVENT_DONE:
                        normalized = ev.response
                if normalized is None:
                    return {
                        "summary": "sub-agent stream ended without DONE event",
                        "exit_reason": "error",
                        "iterations": iterations,
                    }
            else:
                normalized = chain.call(
                    model=model,
                    messages=messages,
                    tools=tools_schema,
                )
        except StreamCancelled:
            return {
                "summary": "sub-agent cancelled mid-stream",
                "exit_reason": "interrupted",
                "iterations": iterations,
            }
        except FailoverExhausted as exc:
            return {
                "summary": f"sub-agent transport failover exhausted: {exc}",
                "exit_reason": "error",
                "iterations": iterations,
            }
        except ValueError as exc:
            return {
                "summary": f"sub-agent received invalid response shape: {exc}",
                "exit_reason": "error",
                "iterations": iterations,
            }
        except Exception as exc:  # noqa: BLE001 — 子 loop 必须吃所有异常，不能炸到父
            logger.exception("[child_loop] unhandled exception: %s", exc)
            return {
                "summary": f"sub-agent crashed: {type(exc).__name__}: {exc}",
                "exit_reason": "error",
                "iterations": iterations,
            }

        # 回填 assistant 消息（与父 main.py 同款 shape，包含 reasoning_content
        # padding 和 content=None 抢救）—— 让 DeepSeek/Kimi thinking 模式下下一轮请求不会 400
        assistant_dump = build_assistant_history_msg(normalized)
        messages.append(assistant_dump)

        if not normalized.tool_calls:
            last_text = normalized.content or ""
            return {
                "summary": last_text.strip(),
                "exit_reason": "completed",
                "iterations": iterations,
            }

        # 跑工具 —— 子永远走 registry.dispatch，绕过 memory_manager
        # 白名单二次校验：防 LLM 幻觉调用未授权工具（罕见但要兜底）
        # V23.3: 每个 tool 跑前先 check cancel —— 父 cancel 时让长时工具
        # （如 terminal 跑 `sleep 100`）不再继续执行
        for tool_call in normalized.tool_calls:
            if _is_cancelled():
                return {
                    "summary": "sub-agent cancelled between tool calls",
                    "exit_reason": "interrupted",
                    "iterations": iterations,
                }
            name = tool_call.function.name
            if name not in allowed_tool_names:
                result = tool_error(
                    f"tool '{name}' is not available to this sub-agent",
                    available=sorted(allowed_tool_names),
                )
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as exc:
                    result = tool_error(f"invalid tool arguments JSON: {exc}")
                else:
                    # registry.dispatch 自带 V21.4 兜底（异常 / 非 str / 非 JSON）
                    result = registry.dispatch(name, args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # 走到这里 = 跑完 max_iterations 仍没自然终止 —— 取最后一条 assistant 的 content
    # 作为 partial summary（可能为空字符串，let it be）
    last_assistant = next(
        (m for m in reversed(messages) if m.get("role") == "assistant"),
        None,
    )
    if last_assistant:
        last_text = last_assistant.get("content") or ""

    return {
        "summary": (
            last_text.strip()
            or f"sub-agent reached max_iterations ({max_iterations}) without producing a final summary"
        ),
        "exit_reason": "max_iterations",
        "iterations": iterations,
    }
