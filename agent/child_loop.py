"""V23.0 / V23.3 / V23.4 — 子 agent loop。

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

各档新增字段 / 路径的"为什么"详见 [docs/decisions/](../../docs/decisions/)。

不做的事（vs 父 loop）
----------------------
- 不调 ``memory_manager.*`` —— 子默认拿不到 memory 工具（隔离原则）
- 不调 ``compressor.*`` —— 子任务短小，max_iterations=8 兜底，不需要压缩
- 不打印任何 UI —— stdout 静默；progress 仅经 callback 走 stderr 侧路
- 不做 prefetch / sync_turn / on_session_switch —— 子无会话生命周期
- 不存完整 tool_result 内容 —— 仅 result_bytes 摘要；完整 trajectory 留 v24

对应源项目
----------
- 子 loop 主体：``tools/delegate_tool.py:1305-1593`` 的 ``_run_single_child``
  → nano 版裁掉 ThreadPoolExecutor、心跳、ACP transport、诊断 dump、
    文件读写跟踪 ~6 块
- V23.4 tool_trace：``tools/delegate_tool.py:1621-1653`` → nano 简化为子内
  trace 数组，dispatch 后 append（不 post-hoc 从 messages 重建）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from tools.registry import ToolRegistry
from tools.result import tool_error
from transports.chain import FailoverExhausted, TransportChain
from transports.streaming import EVENT_DONE, CancelToken, StreamCancelled, StreamEvent
from transports.types import Usage, build_assistant_history_msg

logger = logging.getLogger(__name__)


# 子 system prompt 模板 —— 与父三段式 PromptBuilder 完全独立。
#
# 关键差异：
#   1. 不含父的角色 / 风格指令（避免父 prompt 被复刻进子上下文）
#   2. 不含 skill 索引段 —— 子默认拿不到 skill_view，索引也无意义
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


# tool_trace.args_preview 截断长度。8 轮工具调用 × 200 字节 ≈ 2KB —— 8x
# 这个数会撑爆父 LLM 的 tool_result 上下文预算。改这个常数前先看 trace 总
# 大小是否仍可控。
_ARGS_PREVIEW_MAX = 200


def _truncate_args(raw: str) -> str:
    """args_preview 截断 —— 超过 200 字节加 ``...`` 省略号。"""
    if raw is None:
        return ""
    if len(raw) <= _ARGS_PREVIEW_MAX:
        return raw
    return raw[:_ARGS_PREVIEW_MAX] + "..."


def _classify_tool_status(result: str) -> str:
    """从 V21.4 协议字符串判断工具调用是 ok 还是 error。

    V21.4 ``tool_result(output=...)`` 一定是 ``{"output": ...}``；
    ``tool_error(...)`` 一定是 ``{"error": ..., ...}``。json.loads 后看
    顶层是否含 ``"error"`` 键即可。

    JSON 解析失败 → 视作 ok（保守策略：让"已经合法返回的工具"不被误判
    error；真正破坏协议的字符串会被 ``registry.dispatch`` 自身的 V21.4
    兜底转换成合法 JSON）。
    """
    if not result:
        return "ok"
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return "ok"
    if isinstance(parsed, dict) and "error" in parsed:
        return "error"
    return "ok"


def _accumulate_usage(target: dict[str, int], usage: Optional[Usage]) -> None:
    """把一条 ``Usage`` 累加到 4 维 token dict（mutate target）。

    与 ``transports.types.Usage`` 字段名映射:
    - prompt_tokens          → input
    - completion_tokens      → output
    - cached_tokens          → cache_read（Anthropic 命中 cache 的 input）
    - cache_creation_tokens  → cache_write（Anthropic 写 cache 的 input）
    """
    if usage is None:
        return
    target["input"] += int(usage.prompt_tokens or 0)
    target["output"] += int(usage.completion_tokens or 0)
    target["cache_read"] += int(usage.cached_tokens or 0)
    target["cache_write"] += int(usage.cache_creation_tokens or 0)


def _build_result(
    *,
    summary: str,
    exit_reason: str,
    iterations: int,
    tokens: dict[str, int],
    tool_trace: list[dict],
    started_at: float,
    messages: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """构造 child loop 返回 dict —— 9 处 return 路径共用，避免新加字段漏填。

    V25.0 新增 ``messages``：子内层完整对话（含 system/user/assistant/tool）。
    delegate hook 拿它转 ShareGPT 落子独立 trajectory（决策 6）—— ``tool_trace``
    只是轻量摘要（tool/args_preview/result_bytes/status），转不出合格训练样本。
    返回前 ``list(...)`` 浅拷贝：防调用方 mutate 串改子内部 list（与 tokens 同款保护）。
    早退路径（白名单拒绝 / 构造失败）可能传 None —— 落空列表，hook 端按"无步可落"跳过。
    """
    return {
        "summary": summary,
        "exit_reason": exit_reason,
        "iterations": iterations,
        "tokens": dict(tokens),  # 复制 —— 防调用方 mutate 影响日志
        "tool_trace": list(tool_trace),
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "messages": list(messages) if messages else [],
    }


def run_child_loop(
    *,
    goal: str,
    context: str,
    chain: TransportChain,
    model: str,
    registry: ToolRegistry,
    allowed_tool_names: set[str],
    max_iterations: int = 16,
    cancel_token: Optional[CancelToken] = None,
    stream_enabled: bool = False,
    progress_callback: Optional[Callable[[StreamEvent], None]] = None,
) -> dict[str, Any]:
    """跑一个隔离的子 agent loop，返回结果 dict。

    返回值 schema::

        {
            "summary": str,             # 子最后一条 assistant 文本
            "exit_reason": str,         # "completed" | "max_iterations" | "error" | "interrupted"
            "iterations": int,          # 实际跑了多少轮 LLM 调用
            "tokens": {                 # 子内层 LLM 调用累计 token —— 4 维
                "input": int,
                "output": int,
                "cache_read": int,
                "cache_write": int,
            },
            "tool_trace": [             # 工具调用轻量摘要 —— 每条 dispatch 后追加
                {"tool": str, "args_preview": str, "result_bytes": int, "status": "ok"|"error"},
                ...
            ],
            "duration_seconds": float,  # 子内层 wall-clock 时长
        }

    每条 ``return`` 都走 ``_build_result`` 统一构造，避免 7 处终止路径漏字段。

    流式路径
    --------
    - ``stream_enabled=True`` + ``chain.stream_call`` 可用 → 走流式：每收到
      ``StreamEvent`` 就调一次 ``progress_callback``（侧路给父 stderr 用），
      ``done`` 帧的 ``response`` 字段就是等价的 ``NormalizedResponse``。
    - ``stream_enabled=False`` 或 chain 没 ``stream_call`` → 走 V23.0 同步
      ``chain.call``。这条退化路径让 V23.0/V23.1 fake chain 仍能跑。

    中断路径
    --------
    - 父子共享同一个 ``cancel_token``。
    - 每次调 LLM 前 + 工具循环里每个 tool 跑前 ``check()``。
    - ``StreamCancelled`` 在子层被 catch 后翻译为 ``exit_reason="interrupted"``，
      **不**重抛到父（让 delegate handler 能聚合兄弟子的结果）。

    隔离保证
    --------
    - 调用方传入的 ``messages`` 不被 mutate —— 子在内部新建 list
    - 子 system prompt 不含父的 skill 索引段
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

    # 流式路径只在两个条件都成立时启用 —— ``stream_enabled`` 且 chain 有
    # ``stream_call`` 方法。fake chain（测试用）默认没实现 stream_call，
    # 所以即便测试里漏传 stream_enabled=False 也能优雅退化。
    use_stream = bool(stream_enabled) and hasattr(chain, "stream_call")

    def _is_cancelled() -> bool:
        return cancel_token is not None and cancel_token.is_cancelled()

    # 子内层累计的 token / trace / 起始时刻 —— 跨循环迭代累加
    tokens: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tool_trace: list[dict] = []
    started_at = time.monotonic()

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
            return _build_result(
                summary="sub-agent cancelled before LLM call",
                exit_reason="interrupted",
                iterations=iterations - 1,  # 这一轮没真跑
                tokens=tokens,
                tool_trace=tool_trace,
                started_at=started_at,
                messages=messages,
            )

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
                    return _build_result(
                        summary="sub-agent stream ended without DONE event",
                        exit_reason="error",
                        iterations=iterations,
                        tokens=tokens,
                        tool_trace=tool_trace,
                        started_at=started_at,
                        messages=messages,
                    )
            else:
                normalized = chain.call(
                    model=model,
                    messages=messages,
                    tools=tools_schema,
                )
        except StreamCancelled:
            return _build_result(
                summary="sub-agent cancelled mid-stream",
                exit_reason="interrupted",
                iterations=iterations,
                tokens=tokens,
                tool_trace=tool_trace,
                started_at=started_at,
                messages=messages,
            )
        except FailoverExhausted as exc:
            # v0.28.0: exc_info=True 展开 __cause__（最后一家原始异常）到结构化日志。
            logger.error("[child_loop] transport failover exhausted: %s", exc, exc_info=True)
            return _build_result(
                summary=f"sub-agent transport failover exhausted: {exc}",
                exit_reason="error",
                iterations=iterations,
                tokens=tokens,
                tool_trace=tool_trace,
                started_at=started_at,
                messages=messages,
            )
        except ValueError as exc:
            return _build_result(
                summary=f"sub-agent received invalid response shape: {exc}",
                exit_reason="error",
                iterations=iterations,
                tokens=tokens,
                tool_trace=tool_trace,
                started_at=started_at,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 — 子 loop 必须吃所有异常，不能炸到父
            logger.exception("[child_loop] unhandled exception: %s", exc)
            return _build_result(
                summary=f"sub-agent crashed: {type(exc).__name__}: {exc}",
                exit_reason="error",
                iterations=iterations,
                tokens=tokens,
                tool_trace=tool_trace,
                started_at=started_at,
                messages=messages,
            )

        # 把本次 LLM usage 累加到子内层 tokens（流式 done 帧的 response.usage
        # 已经被 transport 层标准化，等价同步路径）
        _accumulate_usage(tokens, normalized.usage)

        # 回填 assistant 消息（与父 main.py 同款 shape，包含 reasoning_content
        # padding 和 content=None 抢救）—— 让 DeepSeek/Kimi thinking 模式下下一轮请求不会 400
        assistant_dump = build_assistant_history_msg(normalized)
        messages.append(assistant_dump)

        if not normalized.tool_calls:
            last_text = normalized.content or ""
            return _build_result(
                summary=last_text.strip(),
                exit_reason="completed",
                iterations=iterations,
                tokens=tokens,
                tool_trace=tool_trace,
                started_at=started_at,
                messages=messages,
            )

        # 跑工具 —— 子永远走 registry.dispatch，绕过 memory_manager
        # 白名单二次校验：防 LLM 幻觉调用未授权工具（罕见但要兜底）
        # 每个 tool 跑前先 check cancel —— 父 cancel 时让长时工具
        # （如 terminal 跑 `sleep 100`）不再继续执行；dispatch 后把摘要 append 到 tool_trace
        for tool_call in normalized.tool_calls:
            if _is_cancelled():
                return _build_result(
                    summary="sub-agent cancelled between tool calls",
                    exit_reason="interrupted",
                    iterations=iterations,
                    tokens=tokens,
                    tool_trace=tool_trace,
                    started_at=started_at,
                    messages=messages,
                )
            name = tool_call.function.name
            raw_args = tool_call.function.arguments or ""
            if name not in allowed_tool_names:
                result = tool_error(
                    f"tool '{name}' is not available to this sub-agent",
                    available=sorted(allowed_tool_names),
                )
            else:
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError as exc:
                    result = tool_error(f"invalid tool arguments JSON: {exc}")
                else:
                    # registry.dispatch 自带兜底（异常 / 非 str / 非 JSON）
                    result = registry.dispatch(name, args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })
            # 每条 tool 调用追加一条轻量 trace 摘要
            tool_trace.append({
                "tool": name,
                "args_preview": _truncate_args(raw_args),
                "result_bytes": len((result or "").encode("utf-8")),
                "status": _classify_tool_status(result),
            })

    # 走到这里 = 跑完 max_iterations 仍没自然终止 —— 取最后一条 assistant 的 content
    # 作为 partial summary（可能为空字符串，let it be）
    last_assistant = next(
        (m for m in reversed(messages) if m.get("role") == "assistant"),
        None,
    )
    if last_assistant:
        last_text = last_assistant.get("content") or ""

    return _build_result(
        summary=(
            last_text.strip()
            or f"sub-agent reached max_iterations ({max_iterations}) without producing a final summary"
        ),
        exit_reason="max_iterations",
        iterations=iterations,
        tokens=tokens,
        tool_trace=tool_trace,
        started_at=started_at,
        messages=messages,
    )


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
