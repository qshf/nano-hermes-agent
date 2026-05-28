"""V23.0 — ``delegate_task`` 工具：父 agent 派发隔离的子 agent。

教学定位
========
前 22 档全部是单 agent loop。V23.0 第一次引入"父子两个 loop 同进程共存"
的形态，作为多智能体系列的最小可用切片：

- 父 agent 在 tool calling 阶段决定要不要委派 —— ``delegate_task`` 是普通
  LLM 工具（不是 slash 命令）
- 工具 handler 在父进程**同步**跑一个子 ``run_child_loop``：
  - fresh messages（不继承父对话历史 → 隔离原则核心）
  - 聚焦的子 system prompt（仅含 goal + context）
  - 受限工具集 = 父全集 - 黑名单 ``{delegate_task, memory_*}``
  - 共享父的 ``TransportChain`` —— V20 cache、V19 断路器状态全局共享
- 子 loop 跑到自然终止 / max_iterations / 内部错误后返回 dict
- handler 把 dict 的 ``summary`` 字段包装成 ``tool_result(output=...)`` 返回父

为什么走 setter 注入（仿 skill_view_tool）
==========================================
``tools/__init__.py`` 在 import 期就 ``discover_tools()`` 触发本模块的
``registry.register(...)``，但此时 ``main.py`` 还没构建 chain / 还没决定
parent toolsets。我们需要 handler 在被调用时拿到这些运行期对象，所以用
模块级 singleton + ``set_delegate_context()``：

    main.py 启动顺序里在构建完 chain 后调一次 ``set_delegate_context(...)``，
    把 chain / model / parent_tool_names 注入。注入前 handler 返回 error
    （让"父没启用 delegate"的部署仍能跑而不崩）。

不做的事（V23.0 范围）
----------------------
- 不做 ``tasks: []`` 数组（V23.1）
- 不做 ``tools`` schema 字段（V23.1 白名单交集）
- 不做流式 / cancel token 桥接（V23.2）
- 不做结构化结果 / 成本聚合（V23.3）
- 不做 ``role: orchestrator`` / max_spawn_depth（V23.4）
- 不做超时 —— max_iterations=8 是唯一兜底

对应源项目
----------
- 工具 schema：``tools/delegate_tool.py:2626-2743`` 的极简子集（仅 goal/context）
- 黑名单常量：``tools/delegate_tool.py:40-48`` 的 ``DELEGATE_BLOCKED_TOOLS``
  → nano 取 ``delegate_task / memory_*`` 两类（``clarify / send_message /
    execute_code`` 在 nano 不存在）
- handler 主体：``tools/delegate_tool.py:1898-2289`` 的单任务路径精简版
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from tools.registry import registry
from tools.result import tool_error, tool_result

logger = logging.getLogger(__name__)


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


def _resolve_child_toolset() -> set[str]:
    """从父全集 - 黑名单（精确名 + 前缀）得到子允许集。

    分离成函数是为了让测试能直接 mock ``_parent_toolset_names`` 后验证
    黑名单逻辑 —— 不必跑完整 set_delegate_context + chain 构建。
    """
    if _parent_toolset_names is None:
        return set()
    allowed = set()
    for name in _parent_toolset_names:
        if name in _DELEGATE_BLACKLIST_NAMES:
            continue
        if any(name.startswith(p) for p in _DELEGATE_BLACKLIST_PREFIXES):
            continue
        allowed.add(name)
    return allowed


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate a focused, self-contained subtask to an isolated sub-agent. "
        "The sub-agent runs with a fresh conversation (your history is not shared), "
        "a restricted toolset (no nested delegation, no memory writes), and its own "
        "iteration budget. It returns a concise text summary. "
        "Use this when a task has a clean boundary (analyze a file, summarize logs, "
        "extract structured data) — not for chatty back-and-forth or for steps that "
        "need your full context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "The specific objective for the sub-agent. Be concrete and "
                    "scoped — vague goals waste iterations."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional background that the sub-agent needs but cannot infer "
                    "(file paths, prior findings, constraints). Keep it tight; "
                    "the sub-agent does not have access to your conversation."
                ),
            },
        },
        "required": ["goal"],
    },
}


def delegate_task_handler(args: dict) -> str:
    """同步派发一个子 agent，返回 ``tool_result(output=summary)``。

    handler 自身**不**抛异常出去（V21.4 dispatch 兜底虽然能 catch，但语义上
    delegate 失败应该是"工具返回 error"而不是"工具崩了"，让父 LLM 能在
    下一轮自然处理 —— 比如改换策略或道歉）。
    """
    if _chain is None or _model is None or _parent_toolset_names is None:
        return tool_error(
            "delegate_task: not initialized "
            "(set_delegate_context() must be called by main.py before use)"
        )

    goal = (args.get("goal") or "").strip()
    if not goal:
        return tool_error("Parameter 'goal' is required and must be non-empty.")

    context = args.get("context") or ""
    if not isinstance(context, str):
        return tool_error("Parameter 'context' must be a string if provided.")

    allowed = _resolve_child_toolset()
    if not allowed:
        return tool_error(
            "delegate_task: no tools available to sub-agent after blacklist "
            "filtering — refusing to spawn an agent that can do nothing."
        )

    # 局部 import 避免 ``tools/`` ↔ ``agent/`` 子包之间的隐式循环依赖
    # （tools/__init__.py 的 discover_tools 会先 import 本模块，此时 agent
    # 子包可能还在初始化。运行期 import 时序已稳定，安全）。
    from agent.child_loop import run_child_loop

    logger.info(
        "[delegate] spawning child: goal=%r tools=%d",
        goal[:80], len(allowed),
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
        "[delegate] child done: exit=%s iters=%d summary_len=%d",
        result["exit_reason"], result["iterations"], len(result["summary"]),
    )

    # V23.0 仍按 V21.4 工具协议返回纯字符串 ``tool_result(output=...)``。
    # V23.3 才把 summary / exit_reason / tokens / tool_trace 一起塞进 output
    # （仍是单个 JSON 字符串，但内层是 JSON object — 父 LLM 可以选择 json.loads
    # 取细粒度字段）。这一档保持极简：父只看到一段文本。
    return tool_result(output=result["summary"])


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
