"""V23.0 / V23.1 / V23.3 / V23.4 — ``delegate_task`` 工具：父 agent 派发隔离的子 agent。

教学定位（V23.0 → V23.4 弧线）
==============================
- V23.0 父子隔离最小切片（单任务、同步、黑名单写死）
- V23.1 批量并行 + per-task 工具白名单（``ThreadPoolExecutor`` + ``tools`` 字段）
- V23.3 流式中继 + 父子 ``CancelToken`` 桥接
  - 父子共享同一份 ``cancel_token``：父 Ctrl+C → 所有子立刻退出
  - 子 ``chain.stream_call`` 的事件经 ``progress_callback`` 中继到父 stderr
- V23.4 结构化结果 + 成本聚合（**本档**）
  - handler 永远返回 ``tool_result(output=json.dumps({"results":[...]}))``
    —— 单任务 = 含 1 条的 results 数组；父 LLM 永远 ``json.loads`` 二次解析
  - 每条 result 含 ``status`` / ``summary`` / ``tokens`` / ``tool_trace`` /
    ``duration_seconds`` / ``iterations`` / ``exit_reason`` 完整字段
  - 子 worker 完成后把 child tokens 累加到 ``ctx.runtime.session_tokens``
    （模块级 ``_SESSION_TOKENS_LOCK`` 保护，与 progress lock 同款模式）

各档协议升级 / 设计权衡的"为什么"详见 [docs/decisions/](../../docs/decisions/)。

为什么走 setter 注入（仿 skill_view_tool）
==========================================
``tools/__init__.py`` 在 import 期就 ``discover_tools()`` 触发本模块的
``registry.register(...)``，但此时 ``main.py`` 还没构建 chain / 还没决定
parent toolsets。我们需要 handler 在被调用时拿到这些运行期对象，所以用
模块级 singleton + ``set_delegate_context()``：

    main.py 启动顺序里在构建完 chain 后调一次 ``set_delegate_context(...)``，
    把 ``DelegateContext`` 注入。注入前 handler 返回 error
    （让"父没启用 delegate"的部署仍能跑而不崩）。

V23.3 起上下文可挂共享 ``AgentRuntime``（``cancel_token`` / ``stream_enabled``
/ V23.4 的 ``session_tokens``）。没挂 → 子走 V23.0 同步路径（向下兼容；
测试里 fake chain 不需要也能跑）。

不做的事（V23.5+ 仍延期）
------------------------
- 不做 ``role: orchestrator`` / max_spawn_depth（V23.5）
- 不做超时 —— max_iterations=8 是唯一兜底；status 暂不暴露 ``timeout`` 态
- 不做 ``files_read`` / ``files_written`` 跟踪（依赖 file_state，nano 没有）
- 不做 ``output_tail`` / ``model`` / ``api_calls`` / ``_child_role`` 字段
- **不做 FilteredToolRegistry 包装类** —— V23.0 的 ``run_child_loop``
  已经把 ``allowed_tool_names: set[str]`` 当一等参数传入

对应源项目
----------
- V23.0 单任务 schema：``tools/delegate_tool.py:2626-2743`` 极简子集
- V23.1 批量分发：``tools/delegate_tool.py:2071-2193``
- V23.1 工具白名单交集：``tools/delegate_tool.py:940-963``
- V23.3 progress callback：``tools/delegate_tool.py:678-862`` 的
  ``_build_child_progress_callback`` → nano 版只保 stderr sink
- V23.3 中断传播：``tools/delegate_tool.py:2104-2139`` 父 cancel 检测 + 子 token
  设置 → nano 直接共享 V22 的 CancelToken
- V23.3 StreamCancelled 翻译：``tools/delegate_tool.py:1802-1824``
- V23.4 result dict：``tools/delegate_tool.py:1668-1800``
- V23.4 成本聚合：``tools/delegate_tool.py:2231-2279``
- 黑名单常量：``tools/delegate_tool.py:40-48`` → nano 取 ``delegate_task /
  memory_*`` 两类
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from agent.runtime import AgentRuntime, SESSION_TOKEN_KEYS
from tools.registry import registry
from tools.result import tool_error, tool_result
from transports.chain import TransportChain
from transports.streaming import EVENT_DONE, EVENT_TOOL_CALL_STARTED, StreamEvent

if TYPE_CHECKING:
    from agent.runtime_phase import PhaseCloseStatus, PhaseSpan

logger = logging.getLogger(__name__)


# 串行化多 worker 并发写父 stderr（否则单行会被交织截断）。
_PROGRESS_STDERR_LOCK = threading.Lock()


# 串行化多 worker 并发累加 runtime.session_tokens（dict += 非原子）。
_SESSION_TOKENS_LOCK = threading.Lock()


# 并发上限（env DELEGATE_MAX_CONCURRENT 可调，schema 不暴露，默认 3）。
def _get_max_concurrent() -> int:
    raw = os.environ.get("DELEGATE_MAX_CONCURRENT", "3")
    try:
        n = int(raw)
        return n if n > 0 else 3
    except ValueError:
        return 3


# 黑名单（硬编码，即便父暴露 tools 字段后仍强制减去）：
# - delegate_task：防递归
# - memory / memory_* 前缀：防子写脏父知识图谱
_DELEGATE_BLACKLIST_NAMES = frozenset({"delegate_task", "memory"})
_DELEGATE_BLACKLIST_PREFIXES = ("memory_",)


@dataclass
class DelegateContext:
    """Runtime dependencies needed by ``delegate_task``.

    ``runtime`` 是父子共享的真理源（``cancel_token`` / ``stream_enabled``）。
    缺省构造一个全关的 runtime —— 让"父没启用流式 / 中断"的测试与部署仍能跑。
    """

    chain: TransportChain
    model: str
    parent_toolset_names: set[str]
    runtime: AgentRuntime = field(
        default_factory=lambda: AgentRuntime(stream_enabled=False, cancel_token=None)
    )

    def __post_init__(self) -> None:
        # 容忍 list / tuple — 调用方常以列表形式构造，set 化让黑名单逻辑统一
        if not isinstance(self.parent_toolset_names, set):
            self.parent_toolset_names = set(self.parent_toolset_names)


# ── 模块级注入上下文（main.py 启动时填）────────────────────────────────────
_delegate_context: Optional[DelegateContext] = None


def set_delegate_context(context: DelegateContext, /) -> None:
    """main.py 在构建完 chain + 决定 ENABLED_TOOLSETS 后调一次。

    传入一个 ``DelegateContext``，其中 ``parent_toolset_names`` 应是父全集
    （含 ``memory_*`` —— 让黑名单逻辑自己过滤）。子 agent 收到的工具白名单
    = 父全集 - 黑名单。

    流式 / 中断由 ``context.runtime`` 控制；不挂 runtime（默认全关）即走
    V23.0 同步路径，向下兼容旧 fake chain。
    """
    global _delegate_context
    _delegate_context = context


def _resolve_child_toolset(requested: Optional[list[str]] = None) -> set[str]:
    """从父全集 - 黑名单（精确名 + 前缀）得到子允许集；可选传入 ``requested``
    白名单做交集。

    分离成函数是为了让测试能直接注入轻量 context 后验证黑名单逻辑 ——
    不必跑完整 main.py 启动流程。

    V23.1 新增 ``requested`` 参数（来自 schema ``tools`` 字段）：

    - ``requested=None`` → 子拿"父全集 - 黑名单"（V23.0 行为，向下兼容）
    - ``requested=[...]`` → 子拿 ``requested ∩ 父全集 - 黑名单``
      - 用户写了父没装的工具（如 ``"nonexistent"``）→ 静默丢弃，不报错
        （父 LLM 偶尔幻觉工具名，给个 hard error 反而让父连重试机会都没有）
      - 用户写了黑名单（如 ``"delegate_task"``）→ 强制减去
      - 用户传空 list ``[]`` → 子拿到空集，handler 上层会拒绝 spawn
    """
    if _delegate_context is None:
        return set()
    parent_full = _delegate_context.parent_toolset_names
    if requested is None:
        candidates = parent_full
    else:
        # 交集 — 用户传的名字必须真的在父全集里才算数
        candidates = parent_full & set(requested)
    allowed = set()
    for name in candidates:
        if name in _DELEGATE_BLACKLIST_NAMES:
            continue
        if any(name.startswith(p) for p in _DELEGATE_BLACKLIST_PREFIXES):
            continue
        allowed.add(name)
    return allowed


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate one or more focused, self-contained subtasks to isolated sub-agents. "
        "Each sub-agent runs with a fresh conversation (your history is not shared), "
        "a restricted toolset (no nested delegation, no memory writes), and its own "
        "iteration budget. Single-task mode returns the sub-agent's text summary; "
        "batch mode runs tasks in parallel and returns a JSON array (one entry per task). "
        "Use this when tasks have clean boundaries (analyze a file, summarize logs, "
        "extract structured data) — not for chatty back-and-forth or steps that need "
        "your full context. For independent tasks, prefer batch mode (set 'tasks') to "
        "avoid serial round-trips."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # 单任务字段
            "goal": {
                "type": "string",
                "description": (
                    "Single-task mode: the specific objective for the sub-agent. "
                    "Be concrete and scoped — vague goals waste iterations. "
                    "Mutually exclusive with 'tasks'."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Single-task mode: optional background the sub-agent needs but "
                    "cannot infer (file paths, prior findings, constraints). Keep it tight."
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Single-task mode: optional whitelist of tool names the sub-agent "
                    "may use. Final allowed set = whitelist ∩ parent's tools − blacklist. "
                    "Omit to give the sub-agent the full non-blacklisted parent toolset."
                ),
            },
            # 批量模式
            "tasks": {
                "type": "array",
                "description": (
                    "Batch mode: array of subtasks to run in parallel (up to "
                    "DELEGATE_MAX_CONCURRENT workers, default 3). Each entry has its "
                    "own goal/context/tools. Mutually exclusive with top-level 'goal'. "
                    "Results are returned as a JSON array, each entry tagged with "
                    "'task_index' matching the input order."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "The objective for this sub-agent.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Optional background for this sub-agent.",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional per-task tool whitelist. Same intersection "
                                "semantics as top-level 'tools'."
                            ),
                        },
                    },
                    "required": ["goal"],
                },
            },
        },
    },
}


def _validate_task_entry(entry: Any, idx: Optional[int] = None) -> Optional[str]:
    """校验单个 task dict 的结构。返回 None = 合法；返回字符串 = 错误描述。

    分离成函数让单任务路径（顶层字段）和批量路径（tasks[i]）共用同一套校验
    语义；同时让 V23.1 测试 #5（"构建期错不污染并发"）能精准触发各分支。
    """
    prefix = f"tasks[{idx}]" if idx is not None else "task"
    if not isinstance(entry, dict):
        return f"{prefix} must be an object"

    goal = entry.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return f"{prefix}.goal is required and must be a non-empty string"

    context = entry.get("context", "")
    if not isinstance(context, str):
        return f"{prefix}.context must be a string if provided"

    tools = entry.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            return f"{prefix}.tools must be a list of strings if provided"

    return None


def _build_child_progress_callback(
    task_index: Optional[int],
    is_batch: bool,
    stream_enabled: bool,
) -> Optional[Callable[[StreamEvent], None]]:
    """构造一个 stderr progress 中继 callback —— 仅在父侧流式开启时用。

    教学要点（V23.3）
    -----------------
    - 这是工具的**侧路输出通道**：写父 stderr，给"用户"看；
      ``tool_result(output=...)`` 是**主路输出**：返回 JSON，给"父 LLM"看。
      两通道独立，progress 噪音不会污染对话历史。
    - ``text_delta`` / ``reasoning_delta`` **不**逐字符回显（多 worker 并发
      时刷屏 + 行交织都会让人无法读）。只在 ``tool_call_started`` / ``done``
      两个里程碑事件回显一行。
    - 单任务模式不显示 ``[task#N]`` 前缀；批量模式才带索引（避免单任务的
      stderr 看起来突兀）。

    返回 None 表示"不挂 callback" —— 子 loop 会跳过整个 stream 路径走同步
    ``chain.call``（与 V23.0 行为一致）。这样：

    - ``stream_enabled=False`` 部署/测试场景：callback 是 None，子完全走 V23.0
    - 测试代码里完全不需要构造 fake callback，能把"流式中继"和"工具协议"
      两件事的测试解耦
    """
    if not stream_enabled:
        return None

    prefix = f"[task#{task_index}]" if (is_batch and task_index is not None) else "[delegate]"

    def _cb(ev: StreamEvent) -> None:
        # 仅取里程碑事件 —— delta 噪音不打
        if ev.type == EVENT_TOOL_CALL_STARTED:
            line = f"  {prefix} tool: {ev.tool_name}\n"
        elif ev.type == EVENT_DONE:
            # 子完成（无论是文本回答还是要再跑工具，都给父用户一个 hint）
            resp = ev.response
            if resp is None:
                line = f"  {prefix} done\n"
            elif resp.tool_calls:
                line = f"  {prefix} llm-turn done (will run {len(resp.tool_calls)} tool(s))\n"
            else:
                # 终态文本回答 —— 只显示前 60 字符，避免长 summary 刷屏
                head = (resp.content or "").strip().replace("\n", " ")
                if len(head) > 60:
                    head = head[:60] + "..."
                line = f"  {prefix} answer: {head}\n"
        else:
            return  # text_delta / reasoning_delta — 噪音不打

        with _PROGRESS_STDERR_LOCK:
            sys.stderr.write(line)
            sys.stderr.flush()

    return _cb


def _run_one_task(
    entry: dict,
    task_index: Optional[int] = None,
    is_batch: bool = False,
) -> dict:
    """跑一个子任务，返回内部 result dict。

    返回 dict shape::

        {
            "task_index": int,                  # 单任务=0；批量=输入序
            "status": str,                      # completed|max_iterations|interrupted|error
            "exit_reason": str,                 # 同 status（双字段为后续档保留分化空间）
            "summary": str,
            "iterations": int,
            "duration_seconds": float,
            "tokens": {input,output,cache_read,cache_write},
            "tool_trace": [{tool, args_preview, result_bytes, status}, ...],
        }

    本函数被 ``executor.submit`` 调用时跑在 worker 线程；构造期已经在
    主线程完成（``_resolve_child_toolset`` / 校验 / system prompt 渲染都
    不在这），worker 只做"调 LLM + 跑工具"的纯逻辑，不修改父 agent 状态。

    worker 完成后立即把 child tokens 累加到 ``ctx.runtime.session_tokens``：
    每子单独 finalize（不等批量整体结束），即使后续 worker 因 cancel 中断，
    本子已写入的部分不丢失。
    """
    from agent.child_loop import run_child_loop  # 局部 import，见 handler 注释

    # handler 已在入口处保证 _delegate_context 非空，这里直接断言而不是再 fallback
    ctx = _delegate_context
    assert ctx is not None, "delegate_task: context missing — handler must guard"

    # 单任务也用 task_index=0 —— 父 LLM 看到的 ``results`` 永远是数组
    effective_task_index = 0 if task_index is None else task_index

    goal = entry["goal"].strip()
    context = entry.get("context") or ""
    requested = entry.get("tools")  # None 或 list[str]
    allowed = _resolve_child_toolset(requested=requested)
    stream_enabled = ctx.runtime.stream_enabled
    started_at = time.monotonic()

    if not allowed:
        return {
            "task_index": effective_task_index,
            "status": "error",
            "exit_reason": "error",
            "summary": (
                "delegate_task: no tools available to sub-agent after blacklist "
                "filtering — refusing to spawn an agent that can do nothing."
            ),
            "iterations": 0,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "tokens": {k: 0 for k in SESSION_TOKEN_KEYS},
            "tool_trace": [],
        }

    logger.info(
        "[delegate] spawn task_index=%s goal=%r tools=%d stream=%s",
        effective_task_index, goal[:80], len(allowed), stream_enabled,
    )
    progress_cb = _build_child_progress_callback(task_index, is_batch, stream_enabled)
    child_result = run_child_loop(
        goal=goal,
        context=context,
        chain=ctx.chain,
        model=ctx.model,
        registry=registry,
        allowed_tool_names=allowed,
        cancel_token=ctx.runtime.cancel_token,
        stream_enabled=stream_enabled,
        progress_callback=progress_cb,
    )

    # 累加 child tokens 到父 runtime —— worker 线程内立即 finalize，
    # 不等批量整体结束（保证 cancel 时已写入的不丢）。
    _accumulate_runtime_tokens(ctx.runtime, child_result.get("tokens"))

    # 子轨迹 flush：把子内层完整 messages 转 ShareGPT 落一份独立 trajectory。
    # 文件名带 task_index 隔离并发子；completed = 子正常跑完（非 interrupted/error）。
    try:
        from agent.trajectory import flush_session_trajectory
        child_messages = child_result.get("messages") or []
        if child_messages:
            flush_session_trajectory(
                child_messages, model=ctx.model,
                completed=(child_result["exit_reason"] == "completed"),
                filename_stem=f"child_t{effective_task_index}",
            )
    except Exception:  # noqa: BLE001 — 子轨迹落盘永不阻断父对结果的聚合
        logger.warning("[delegate] child trajectory flush failed", exc_info=True)

    exit_reason = child_result["exit_reason"]
    logger.info(
        "[delegate] done task_index=%s exit=%s iters=%d summary_len=%d "
        "tokens_in=%d tokens_out=%d",
        effective_task_index, exit_reason, child_result["iterations"],
        len(child_result["summary"]),
        child_result["tokens"]["input"], child_result["tokens"]["output"],
    )
    return {
        "task_index": effective_task_index,
        "status": exit_reason,
        "exit_reason": exit_reason,
        "summary": child_result["summary"],
        "iterations": child_result["iterations"],
        "duration_seconds": child_result.get(
            "duration_seconds", round(time.monotonic() - started_at, 3),
        ),
        "tokens": child_result["tokens"],
        "tool_trace": child_result["tool_trace"],
    }


def _start_child_agent_phase(*, mode: str, task_count: int) -> "PhaseSpan | None":
    if _delegate_context is None:
        return None
    tracker = getattr(_delegate_context.runtime, "phase_tracker", None)
    if tracker is None:
        return None
    try:
        from agent.runtime_phase import PHASE_CHILD_AGENT_RUNNING
        return tracker.start(
            PHASE_CHILD_AGENT_RUNNING,
            mode=mode,
            task_count=task_count,
            completed_count=0,
            running_count=task_count,
            failed_count=0,
        )
    except Exception:  # noqa: BLE001 — delegate execution must not depend on observers
        logger.debug("delegate child phase start failed", exc_info=True)
        return None


def _update_child_agent_phase(
    span: "PhaseSpan | None",
    *,
    task_count: int,
    completed_count: int,
    failed_count: int,
    mode: str,
) -> None:
    if span is None:
        return
    try:
        span.activity_event(
            mode=mode,
            task_count=task_count,
            completed_count=completed_count,
            running_count=max(0, task_count - completed_count),
            failed_count=failed_count,
        )
    except Exception:  # noqa: BLE001
        logger.debug("delegate child phase update failed", exc_info=True)


def _close_child_agent_phase(
    span: "PhaseSpan | None",
    *,
    status: "PhaseCloseStatus" = "finished",
    **activity: Any,
) -> None:
    if span is None or _delegate_context is None:
        return
    tracker = getattr(_delegate_context.runtime, "phase_tracker", None)
    if tracker is None:
        return
    try:
        tracker.close(span, status=status, **activity)
    except Exception:  # noqa: BLE001
        logger.debug("delegate child phase close failed", exc_info=True)


def _accumulate_runtime_tokens(
    runtime: AgentRuntime, child_tokens: Optional[dict[str, int]],
) -> None:
    """把 child token dict 累加到 ``runtime.session_tokens``（lock 保护）。

    ``child_tokens`` 为 None / 缺字段 时静默跳过对应键 —— 让 fake transport
    测试场景手工构造 partial child_result 时不必补全 4 维。
    """
    if child_tokens is None:
        return
    with _SESSION_TOKENS_LOCK:
        for k in SESSION_TOKEN_KEYS:
            v = child_tokens.get(k)
            if isinstance(v, (int, float)):
                runtime.session_tokens[k] += int(v)


def delegate_task_handler(args: dict) -> str:
    """同步派发一个或多个子 agent，返回 V21.4 工具协议的 JSON 字符串。

    路径分流：

    - ``args`` 含 ``tasks`` → 批量路径（V23.1 新）：主线程校验全部 tasks，
      任一不合法直接 ``tool_error`` 返回（**绝不**部分启动）；全部合法后用
      ``ThreadPoolExecutor.map`` 并行跑，结果按输入顺序聚合到 JSON 数组。
    - ``args`` 含 ``goal`` → 单任务路径（V23.0 兼容）：直接跑 ``_run_one_task``，
      返回 ``tool_result(output=summary_str)``，输出形态完全不变。
    - 两者都没 / 两者都有 → ``tool_error`` 让父 LLM 知道用法。

    handler 自身**不**抛异常出去（V21.4 dispatch 兜底虽然能 catch，但语义上
    delegate 失败应该是"工具返回 error"而不是"工具崩了"，让父 LLM 能在
    下一轮自然处理 —— 比如改换策略或道歉）。
    """
    if _delegate_context is None:
        return tool_error(
            "delegate_task: not initialized "
            "(set_delegate_context() must be called by main.py before use)"
        )

    has_goal = "goal" in args and args["goal"] is not None
    has_tasks = "tasks" in args and args["tasks"] is not None

    if has_goal and has_tasks:
        return tool_error(
            "delegate_task: 'goal' and 'tasks' are mutually exclusive — "
            "use 'goal' for a single task or 'tasks' for a batch."
        )
    if not has_goal and not has_tasks:
        return tool_error(
            "delegate_task: must provide either 'goal' (single task) or "
            "'tasks' (batch of tasks)."
        )

    overall_started_at = time.monotonic()

    # ─── 单任务路径 ─────────────────────────────────────────────────────────
    if has_goal:
        single_entry = {
            "goal": args.get("goal"),
            "context": args.get("context") or "",
            "tools": args.get("tools"),
        }
        err = _validate_task_entry(single_entry, idx=None)
        if err:
            return tool_error(err)
        _child_phase = _start_child_agent_phase(mode="single", task_count=1)
        try:
            result = _run_one_task(single_entry, task_index=None, is_batch=False)
        except Exception as exc:  # noqa: BLE001 — handler keeps V21.4 tool protocol shape
            _close_child_agent_phase(_child_phase, status="error", mode="single", task_count=1, completed_count=0, running_count=0, failed_count=1)
            logger.exception("[delegate] single task failed")
            return tool_error(f"delegate_task failed: {type(exc).__name__}: {exc}")
        _close_child_agent_phase(_child_phase, mode="single", task_count=1, completed_count=1, running_count=0, failed_count=0)
        # 单任务也走 results 数组（含 1 条），父 LLM 永远 json.loads → r["results"][i]
        return tool_result(output=json.dumps(
            _build_handler_payload(
                results=[result],
                started_at=overall_started_at,
            ),
            ensure_ascii=False,
        ))

    # ─── 批量路径 ───────────────────────────────────────────────────────────
    tasks = args["tasks"]
    if not isinstance(tasks, list):
        return tool_error("delegate_task: 'tasks' must be an array of task objects.")
    if not tasks:
        return tool_error("delegate_task: 'tasks' must not be empty.")

    # 主线程逐条校验 — 任一不合法整体拒绝（fail-fast，绝不半启动）
    for i, t in enumerate(tasks):
        err = _validate_task_entry(t, idx=i)
        if err:
            return tool_error(err)

    max_workers = min(_get_max_concurrent(), len(tasks))
    logger.info(
        "[delegate] batch start tasks=%d max_workers=%d",
        len(tasks), max_workers,
    )

    # Future loop lets the parent report aggregate child-agent progress while waiting.
    _child_phase = _start_child_agent_phase(mode="batch", task_count=len(tasks))
    indexed = list(enumerate(tasks))
    results: list[dict | None] = [None] * len(tasks)
    completed_count = 0
    failed_count = 0
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_index = {
                pool.submit(_run_one_task, entry, task_index=idx, is_batch=True): idx
                for idx, entry in indexed
            }
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                result = future.result()
                results[idx] = result
                completed_count += 1
                if result.get("status") in {"error", "interrupted"}:
                    failed_count += 1
                _update_child_agent_phase(
                    _child_phase,
                    mode="batch",
                    task_count=len(tasks),
                    completed_count=completed_count,
                    failed_count=failed_count,
                )
    except Exception as exc:  # noqa: BLE001 — handler keeps V21.4 tool protocol shape
        _close_child_agent_phase(
            _child_phase,
            status="error",
            mode="batch",
            task_count=len(tasks),
            completed_count=completed_count,
            running_count=0,
            failed_count=max(failed_count, 1),
        )
        logger.exception("[delegate] batch failed")
        return tool_error(f"delegate_task failed: {type(exc).__name__}: {exc}")
    final_results = [r for r in results if r is not None]
    _close_child_agent_phase(
        _child_phase,
        mode="batch",
        task_count=len(tasks),
        completed_count=len(final_results),
        running_count=0,
        failed_count=failed_count,
    )

    logger.info("[delegate] batch done tasks=%d", len(final_results))

    # 单任务 + 批量统一返回 ``{"results":[...]}``；外层 ``tool_result`` 仍是
    # ``{"output": <json_string>}`` 协议，父 LLM 二次 ``json.loads`` 拿 results 数组。
    return tool_result(output=json.dumps(
        _build_handler_payload(results=final_results, started_at=overall_started_at),
        ensure_ascii=False,
    ))


def _build_handler_payload(
    *, results: list[dict], started_at: float,
) -> dict[str, Any]:
    """构造 handler 顶层 payload —— 单任务 / 批量共用。

    顶层 schema::

        {
            "results": [
                {
                    "task_index": int,
                    "status": "completed"|"max_iterations"|"interrupted"|"error",
                    "exit_reason": str,
                    "summary": str,
                    "iterations": int,
                    "duration_seconds": float,
                    "tokens": {input,output,cache_read,cache_write},
                    "tool_trace": [...],
                },
                ...
            ],
            "total_duration_seconds": float,
        }
    """
    return {
        "results": results,
        "total_duration_seconds": round(time.monotonic() - started_at, 3),
    }


def _check_delegate_ready() -> bool:
    """check_fn — 仅当 set_delegate_context 已调用且子能拿到至少 1 个工具时暴露。

    没注入 / 注入后子工具集为空时把工具藏起来：避免 LLM 看到一个永远返回
    error 的工具。教学版部署里如果选择不挂 delegate（"我先体验单 agent"），
    main.py 不调 set_delegate_context 即可，agent 启动 banner / 工具列表自然
    不显示 delegate_task。
    """
    if _delegate_context is None:
        return False
    return len(_resolve_child_toolset()) > 0


registry.register(DELEGATE_TASK_SCHEMA, delegate_task_handler, check_fn=_check_delegate_ready)
