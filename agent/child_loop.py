"""V23.0 — 子 agent loop（最小可用版）。

教学定位
========
父 agent loop 在 ``main.py:run_agent`` 里，是含 memory / compress / streaming UI
的全功能形态。V23.0 引入"父子隔离"概念时面临选择：

A. 把 main.py 的 agent loop 抽成 ``AIAgent`` 类，父子复用同一份代码
B. 写一个**裁剪版** child loop，只保留"调 LLM → 跑工具 → 拼 messages"的核心三步

源项目走 A（``run_agent.py:run_conversation`` 同时被父和子调用，1300+ 行），
因为生产环境父子的所有特性都要等价（流式、cache、failover、trajectory 等）。
nano 走 B —— 子的能力本就受限（黑名单 memory、不流式、不进 UI），裁剪版让
"父子的差异在哪里"在代码层就一目了然，而不是埋在 ``role == "leaf"`` 这种 if 分支里。

V23.2 接入流式 + cancel 时再考虑：
    - 子是否要走 ``chain.stream_call`` —— 如果走，progress callback 中继到父
      stderr；不走，仍是同步 ``chain.call``。
V23.3 加结构化结果 + tool_trace 时，本函数返回值从只含 summary 升级为含
``tokens`` / ``tool_trace`` / ``status`` 的完整 dict（已预留 ``exit_reason`` 字段）。

不做的事（vs 父 loop）
----------------------
- 不调 ``memory_manager.*`` —— 子默认拿不到 memory 工具（隔离原则）
- 不调 ``compressor.*`` —— 子任务短小，max_iterations=8 兜底，不需要压缩
- 不调 ``stream_call`` —— V23.0 同步路径；V23.2 才接流式
- 不打印任何 UI —— stdout/stderr 静默；调试用 logger.debug
- 不做 prefetch / sync_turn / on_session_switch —— 子无会话生命周期

对应源项目
----------
- 子 loop 主体：``tools/delegate_tool.py:1305-1593`` 的 ``_run_single_child``
  → nano 版裁掉 ThreadPoolExecutor、心跳、tool_progress_callback、ACP transport、
    诊断 dump、文件读写跟踪 ~6 块，只剩同步串行 LLM ↔ tool 循环
- 子 system prompt：``tools/delegate_tool.py:564-637`` 的 ``_build_child_system_prompt``
  → nano 版固定模板（不读 config、不按 role 分支、不注入 skill 索引）
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tools.result import tool_error
from transports.chain import FailoverExhausted

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
    chain: Any,                      # transports.chain.TransportChain
    model: str,
    registry: Any,                   # tools.registry.ToolRegistry
    allowed_tool_names: set[str],
    max_iterations: int = 8,
) -> dict:
    """跑一个隔离的子 agent loop，返回结果 dict。

    V23.0 返回值（最小集）::

        {
            "summary": str,         # 子最后一条 assistant 文本（或失败 / 截断说明）
            "exit_reason": str,     # "completed" | "max_iterations" | "error"
            "iterations": int,      # 实际跑了多少轮 LLM 调用
        }

    V23.3 起会扩展 ``tokens`` / ``tool_trace`` / ``status`` 字段，但
    ``summary`` / ``exit_reason`` 是稳定承诺。

    隔离保证（V23.0 测试覆盖）
    --------------------------
    - 调用方传入的 ``messages`` 不被 mutate —— 子在内部新建 list
    - 子 system prompt 不含父的 skill 索引段（构造时就不注入）
    - 子工具白名单 = ``allowed_tool_names`` ∩ ``registry.available_tool_names``
      —— 子永远拿不到父没装的工具，更拿不到黑名单工具
    - 子不持有 ``memory_manager`` 引用 —— 即便 LLM 幻觉调用 memory_*，
      registry.dispatch 会返回 "Unknown tool"，**不会**写到父 memory bank
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
    # 注意：get_definitions 内部还会过 check_fn（registry 已实现 30s TTL 缓存）
    tools_schema = registry.get_definitions(effective_tools)

    iterations = 0
    last_text = ""

    while iterations < max_iterations:
        iterations += 1
        logger.debug("[child_loop] iter=%d goal_prefix=%r", iterations, goal[:60])

        try:
            normalized = chain.call(
                model=model,
                messages=messages,
                tools=tools_schema,
            )
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
        from transports.types import build_assistant_history_msg
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
        for tool_call in normalized.tool_calls:
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
