"""V23.0 / V23.1 — ``delegate_task`` 工具：父 agent 派发隔离的子 agent。

教学定位（V23.0 起点）
======================
前 22 档全部是单 agent loop。V23.0 第一次引入"父子两个 loop 同进程共存"，
作为多智能体系列的最小可用切片。V23.1 在它之上新增两个能力：

- **批量并行**：``tasks: [{goal, context, tools?}, ...]`` 数组 + 主线程构建
  + ``ThreadPoolExecutor`` 并跑（默认 3 worker，env ``DELEGATE_MAX_CONCURRENT``）
- **per-task 工具白名单**：每个任务可选 ``tools: [...]``，与父全集取交集
  并强制减黑名单（即便父 LLM 在 ``tools`` 里写 ``delegate_task``，子也拿不到）

V23.0 的语义保持不变：不传 ``tasks`` 时仍是单任务同步路径；返回值仍是
``tool_result(output=summary_str)``。批量模式返回 JSON 数组，每条带
``task_index``（让父 LLM 不依赖位置去匹配）。

为什么走 setter 注入（仿 skill_view_tool）
==========================================
``tools/__init__.py`` 在 import 期就 ``discover_tools()`` 触发本模块的
``registry.register(...)``，但此时 ``main.py`` 还没构建 chain / 还没决定
parent toolsets。我们需要 handler 在被调用时拿到这些运行期对象，所以用
模块级 singleton + ``set_delegate_context()``：

    main.py 启动顺序里在构建完 chain 后调一次 ``set_delegate_context(...)``，
    把 chain / model / parent_tool_names 注入。注入前 handler 返回 error
    （让"父没启用 delegate"的部署仍能跑而不崩）。

不做的事（V23.1 范围内仍延期）
------------------------------
- 不做流式 / cancel token 桥接（V23.2）
- 不做结构化结果 / 成本聚合（V23.3：``tokens`` / ``tool_trace`` / ``status``）
- 不做 ``role: orchestrator`` / max_spawn_depth（V23.4）
- 不做超时 —— max_iterations=8 是唯一兜底
- **不做 FilteredToolRegistry 包装类** —— V23.0 的 ``run_child_loop``
  已经把 ``allowed_tool_names: set[str]`` 当一等参数传入，相当于现成的
  per-call filter，再开一个类是名词包装

对应源项目
----------
- V23.0 单任务 schema：``tools/delegate_tool.py:2626-2743`` 极简子集
- V23.1 批量分发：``tools/delegate_tool.py:2071-2193`` 主线程构建 →
  ThreadPoolExecutor 提交 → 收集排序
- V23.1 工具白名单交集：``tools/delegate_tool.py:940-963`` 的
  ``_resolve_child_toolsets``
- 黑名单常量：``tools/delegate_tool.py:40-48`` 的 ``DELEGATE_BLOCKED_TOOLS``
  → nano 取 ``delegate_task / memory_*`` 两类（``clarify / send_message /
    execute_code`` 在 nano 不存在）
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from tools.registry import registry
from tools.result import tool_error, tool_result

logger = logging.getLogger(__name__)


# ── V23.1 并发上限（env 可调，schema 不暴露 — 防父 LLM 滥用并发）─────────
#
# 默认 3 来自源项目 ``delegation.max_concurrent_children``。值取小一点的
# 教学考量：3 个子并跑足够展示加速效果，又不至于一次开 8 个让 transport
# rate-limit 触发，把"批量加速 + 故障切换"两个不变量纠缠到一起难以测试。
def _get_max_concurrent() -> int:
    raw = os.environ.get("DELEGATE_MAX_CONCURRENT", "3")
    try:
        n = int(raw)
        return n if n > 0 else 3
    except ValueError:
        return 3


# ── 黑名单（硬编码，V23.1 起暴露 ``tools`` 字段后仍强制减去）──────────────
#
# 每条都有具体理由，不要无差别复制源项目列表（nano 没有那么多工具）：
#
# - delegate_task：防递归（V23.0 仅 1 层；V23.4 才放开 orchestrator 角色）
# - memory：nano 当前 memory_manager 暴露的单一工具名（V8 起），子拿了就能
#           写脏父知识图谱
# - memory_* 前缀：源项目里 hindsight 等 provider 拆出的子工具（如
#           ``memory_recall_v2``）；nano 暂未拆，但前缀兜底未来扩展
_DELEGATE_BLACKLIST_NAMES = frozenset({"delegate_task", "memory"})
_DELEGATE_BLACKLIST_PREFIXES = ("memory_",)


# ── 模块级注入上下文（main.py 启动时填）────────────────────────────────────
_chain: Optional[Any] = None     # transports.chain.TransportChain
_model: Optional[str] = None
_parent_toolset_names: Optional[list[str]] = None  # 父全集（含 memory_*）


def set_delegate_context(
    *,
    chain: Any,
    model: str,
    parent_toolset_names: list[str],
) -> None:
    """main.py 在构建完 chain + 决定 ENABLED_TOOLSETS 后调一次。

    ``parent_toolset_names`` 应该是父全集（含 ``memory_*`` —— 让黑名单逻辑
    自己过滤）。子 agent 收到的工具白名单 = 父全集 - 黑名单。
    """
    global _chain, _model, _parent_toolset_names
    _chain = chain
    _model = model
    _parent_toolset_names = list(parent_toolset_names)


def _resolve_child_toolset(requested: Optional[list[str]] = None) -> set[str]:
    """从父全集 - 黑名单（精确名 + 前缀）得到子允许集；可选传入 ``requested``
    白名单做交集。

    分离成函数是为了让测试能直接 mock ``_parent_toolset_names`` 后验证
    黑名单逻辑 —— 不必跑完整 set_delegate_context + chain 构建。

    V23.1 新增 ``requested`` 参数（来自 schema ``tools`` 字段）：

    - ``requested=None`` → 子拿"父全集 - 黑名单"（V23.0 行为，向下兼容）
    - ``requested=[...]`` → 子拿 ``requested ∩ 父全集 - 黑名单``
      - 用户写了父没装的工具（如 ``"nonexistent"``）→ 静默丢弃，不报错
        （父 LLM 偶尔幻觉工具名，给个 hard error 反而让父连重试机会都没有）
      - 用户写了黑名单（如 ``"delegate_task"``）→ 强制减去
      - 用户传空 list ``[]`` → 子拿到空集，handler 上层会拒绝 spawn
    """
    if _parent_toolset_names is None:
        return set()
    parent_full = set(_parent_toolset_names)
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
            # 单任务字段（V23.0 兼容路径）
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
            # V23.1 新增 — 批量模式
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


def _run_one_task(entry: dict, task_index: Optional[int] = None) -> dict:
    """跑一个子任务，返回内部 result dict。

    内部 dict shape（V23.1 临时版）::

        {
            "task_index": int | None,   # 单任务时 None；批量时来自调用方
            "summary": str,
            "exit_reason": str,         # "completed"|"max_iterations"|"error"
            "iterations": int,
        }

    V23.3 会把 ``tokens`` / ``tool_trace`` / ``status`` 字段一起塞进来；
    本档只保 V23.0 三字段 + ``task_index`` —— 让批量结果数组自带索引，
    父 LLM 不依赖位置即可对齐 ``tasks`` 数组。

    本函数被 ``executor.submit`` 调用时跑在 worker 线程；构造期已经在
    主线程完成（``_resolve_child_toolset`` / 校验 / system prompt 渲染都
    不在这），worker 只做"调 LLM + 跑工具"的纯逻辑，不修改父 agent 状态。
    """
    from agent.child_loop import run_child_loop  # 局部 import，见 handler 注释

    goal = entry["goal"].strip()
    context = entry.get("context") or ""
    requested = entry.get("tools")  # None 或 list[str]
    allowed = _resolve_child_toolset(requested=requested)

    if not allowed:
        return {
            "task_index": task_index,
            "summary": (
                "delegate_task: no tools available to sub-agent after blacklist "
                "filtering — refusing to spawn an agent that can do nothing."
            ),
            "exit_reason": "error",
            "iterations": 0,
        }

    logger.info(
        "[delegate] spawn task_index=%s goal=%r tools=%d",
        task_index, goal[:80], len(allowed),
    )
    result = run_child_loop(
        goal=goal,
        context=context,
        chain=_chain,
        model=_model,
        registry=registry,
        allowed_tool_names=allowed,
    )
    logger.info(
        "[delegate] done task_index=%s exit=%s iters=%d summary_len=%d",
        task_index, result["exit_reason"], result["iterations"], len(result["summary"]),
    )
    return {
        "task_index": task_index,
        "summary": result["summary"],
        "exit_reason": result["exit_reason"],
        "iterations": result["iterations"],
    }


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
    if _chain is None or _model is None or _parent_toolset_names is None:
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

    # ─── 单任务路径（V23.0 兼容）─────────────────────────────────────────
    if has_goal:
        single_entry = {
            "goal": args.get("goal"),
            "context": args.get("context") or "",
            "tools": args.get("tools"),
        }
        err = _validate_task_entry(single_entry, idx=None)
        if err:
            return tool_error(err)
        result = _run_one_task(single_entry, task_index=None)
        # V23.0 协议：单任务仍只把 summary 字符串塞进 output，**不**包 JSON 嵌套
        # （让父 LLM 看到的就是一段文本，与 V23.0 行为完全一致）
        return tool_result(output=result["summary"])

    # ─── 批量路径（V23.1 新）─────────────────────────────────────────────
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

    # ``executor.map`` 保留输入顺序（无论 worker 完成快慢），刚好与
    # iteration-plan §V23.1 验证项 #2"结果顺序与 tasks 数组对齐"对齐。
    # V23.2 接进度中继时再换 ``as_completed``。
    indexed = list(enumerate(tasks))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda it: _run_one_task(it[1], task_index=it[0]), indexed))

    logger.info("[delegate] batch done tasks=%d", len(results))

    # 批量返回 = JSON 数组的 string，仍走 ``tool_result(output=...)`` 单层 JSON
    # 协议（V21.4），父 LLM 看到的 output 字段值是个 JSON 字符串，可 ``json.loads``
    # 二次解析得到结构化数组。这与 V23.3 完整结构化（含 tokens / tool_trace）
    # 的二级嵌套语义一脉相承 —— 现在先把"父能 json.loads"这件事建立起来。
    import json as _json
    payload = [
        {
            "task_index": r["task_index"],
            "summary": r["summary"],
            "exit_reason": r["exit_reason"],
        }
        for r in results
    ]
    return tool_result(output=_json.dumps(payload, ensure_ascii=False))


def _check_delegate_ready() -> bool:
    """check_fn — 仅当 set_delegate_context 已调用且子能拿到至少 1 个工具时暴露。

    没注入 / 注入后子工具集为空时把工具藏起来：避免 LLM 看到一个永远返回
    error 的工具。教学版部署里如果选择不挂 delegate（"我先体验单 agent"），
    main.py 不调 set_delegate_context 即可，agent 启动 banner / 工具列表自然
    不显示 delegate_task。
    """
    if _chain is None or _parent_toolset_names is None:
        return False
    return len(_resolve_child_toolset()) > 0


registry.register(DELEGATE_TASK_SCHEMA, delegate_task_handler, check_fn=_check_delegate_ready)
