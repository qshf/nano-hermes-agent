"""V25.1 — insights：读 v24 SQLite 会话库出跨会话统计报表（数据飞轮读路径）。

这一档解决什么
==============
v25.0 让对话能落成训练样本（写路径），但**看不到聚合视图**：累计 token / 估算
成本 / tool 调用 top-N / 失败率 / 平均轮长全藏在 SQLite 里。v25.1 接住读路径 ——
``InsightsEngine`` 把 v24 的 ``sessions`` / ``messages`` 两表跨会话聚合成报表。

为什么读 v24 SQLite 而不读 trajectory jsonl（决策 4）
=====================================================
**insights 的数据源是 v24 的 SQLite，不是 v25.0 的 trajectory jsonl。** 三条理由：

1. v24 store 现成有 ``input/output/cache_read/cache_write`` 4 列 + ``turn_count`` /
   ``model`` / ``created_at`` / ``end_reason`` —— insights 要的字段全在表里。
2. jsonl 是**有损**训练格式（脱敏改了内容 + 拍平丢了 tool_call_id），拿它算
   token / 成本会因转换失真。SQLite 存的是**原始计量**。
3. 关注点正交：trajectory 写训练、insights 读计量，各取所需。

**因此 v25.1 只依赖 v24，不依赖 v25.0** —— 删光 ``trajectories/`` 后 ``/insights``
仍正常出报表（test 15 验证这条独立性）。这纠正了原 roadmap "用 jsonl 存统计" 的
错误前提（那前提是"v14 有 SQLite"，已被 v24 推翻）。

源项目对照：``hermes-agent/agent/insights.py``（931 行）。nano 砍掉 platform
breakdown（只 CLI）/ skill breakdown / activity 模式（day/hour/streak）/ top
sessions / gateway markdown —— 只做 overview + tool top-N + per-model + 失败率。

成本估算（决策 5）
==================
``estimate_cost`` 内嵌 deepseek + qwen 当前单价（input/output/cache 三档），按 4 维
token 算。源项目的多家 pricing 表 + pricing_version + actual_cost 对账归 v26+。
**价格会过时，是已知简化** —— 真要准确对账再开档。未知 model 记 0 成本 + 标 unknown。
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from typing import Any, Optional

# ── 成本单价表（决策 5：hardcode 当前两家，单位 USD / 1M tokens）────────────
# 按 model 名子串匹配。未知 model → 全 0 + 标 unknown（不瞎猜价）。
# 注：价格随官方调整会过时，这是教学版的已知简化（见模块 docstring）。
_PRICING: dict[str, dict[str, float]] = {
    "deepseek": {"input": 0.27, "output": 1.10, "cache_read": 0.07, "cache_write": 0.27},
    "qwen":     {"input": 0.40, "output": 1.20, "cache_read": 0.10, "cache_write": 0.40},
}
_SECONDS_PER_DAY = 86400


def _match_pricing(model: str) -> Optional[dict[str, float]]:
    """按子串匹配价目表 —— ``deepseek-chat`` → deepseek，``qwen3.6-plus`` → qwen。"""
    m = (model or "").lower()
    for key, prices in _PRICING.items():
        if key in m:
            return prices
    return None


def estimate_cost(model: str, tokens: dict[str, int]) -> tuple[float, str]:
    """按 model 选价、4 维 token 算成本（USD）。

    返回 ``(cost, status)``：status ∈ {"included", "unknown"}。未知 model 无价目
    → 返回 ``(0.0, "unknown")``，让报表把它从成本里剔出来并提示"N 个会话价格未知"。
    """
    prices = _match_pricing(model)
    if prices is None:
        return 0.0, "unknown"
    cost = sum(
        (tokens.get(dim, 0) or 0) / 1_000_000 * prices[dim]
        for dim in ("input", "output", "cache_read", "cache_write")
    )
    return cost, "included"


class InsightsEngine:
    """读 v24 ``SessionStore`` 跨会话聚合统计（决策 4：数据源是 SQLite 不是 jsonl）。

    直接吃一个 ``SessionStore`` 实例，复用它的 ``conn``（只读，不写）。报表四块：
    overview（4 维 token / 估算成本 / 会话数 / 平均轮长）+ tool 调用 top-N +
    per-model breakdown + tool 失败率。
    """

    # 4 维 token 列名 —— 与 session_store._TOKEN_COLUMNS 对齐
    _TOKEN_DIMS = ("input", "output", "cache_read", "cache_write")

    def __init__(self, store: Any) -> None:
        self.store = store
        self.conn = store.conn

    def generate(self, *, days: int = 30, now: Optional[float] = None) -> dict:
        """读 sessions 表（时间窗过滤）+ messages 表（tool_calls / tool 结果），出报表 dict。

        ``days`` 时间窗按 ``created_at >= now - days*86400`` 过滤（决策测 14：窗外不计）。
        ``now`` 缺省取 ``time.time()``；测试传固定值做决定性断言。``days <= 0`` 视为
        "全部"（不设下界）。
        """
        cutoff = 0.0
        if days > 0:
            base = now if now is not None else time.time()
            cutoff = base - days * _SECONDS_PER_DAY

        sessions = self.conn.execute(
            "SELECT session_id, model, turn_count, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, created_at, end_reason "
            "FROM sessions WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
        sids = [s["session_id"] for s in sessions]

        overview = self._overview(sessions)
        per_model = self._per_model(sessions)
        tool_top, tool_fail = self._tool_stats(sids)
        return {
            "days": days,
            "overview": overview,
            "per_model": per_model,
            "tool_top": tool_top,
            "tool_failure": tool_fail,
        }

    # ── 聚合子过程 ──────────────────────────────────────────────────────────
    def _session_tokens(self, s: Any) -> dict[str, int]:
        return {
            "input": s["input_tokens"] or 0,
            "output": s["output_tokens"] or 0,
            "cache_read": s["cache_read_tokens"] or 0,
            "cache_write": s["cache_write_tokens"] or 0,
        }

    def _overview(self, sessions: list) -> dict:
        totals = {dim: 0 for dim in self._TOKEN_DIMS}
        total_cost = 0.0
        unknown_cost_sessions = 0
        total_turns = 0
        for s in sessions:
            tok = self._session_tokens(s)
            for dim in self._TOKEN_DIMS:
                totals[dim] += tok[dim]
            cost, status = estimate_cost(s["model"] or "", tok)
            total_cost += cost
            if status == "unknown":
                unknown_cost_sessions += 1
            total_turns += s["turn_count"] or 0
        n = len(sessions)
        return {
            "total_sessions": n,
            "tokens": totals,
            "total_tokens": sum(totals.values()),
            "estimated_cost": total_cost,
            "unknown_cost_sessions": unknown_cost_sessions,
            "total_turns": total_turns,
            "avg_turns": (total_turns / n) if n else 0.0,
        }

    def _per_model(self, sessions: list) -> list[dict]:
        """按 model 分组：会话数 + 4 维 token 合计 + 成本。按总 token 降序。"""
        groups: dict[str, dict] = defaultdict(
            lambda: {"sessions": 0, "tokens": {d: 0 for d in self._TOKEN_DIMS}, "cost": 0.0}
        )
        for s in sessions:
            model = s["model"] or "unknown"
            tok = self._session_tokens(s)
            g = groups[model]
            g["sessions"] += 1
            for dim in self._TOKEN_DIMS:
                g["tokens"][dim] += tok[dim]
            cost, _ = estimate_cost(model, tok)
            g["cost"] += cost
        out = [
            {"model": m, **g, "total_tokens": sum(g["tokens"].values())}
            for m, g in groups.items()
        ]
        out.sort(key=lambda r: r["total_tokens"], reverse=True)
        return out

    def _tool_stats(self, sids: list[str]) -> tuple[list[tuple[str, int]], dict]:
        """从 messages 解析 tool 使用：

        - top-N：扫 assistant 行的 ``tool_calls`` JSON，按 function.name 计数
        - 失败率：扫 role=tool 行的 ``content``，JSON 含 ``error`` key 即失败
          （对齐 V21.4 ``tool_error`` 协议 ``{"error": ...}``）
        """
        name_counter: Counter = Counter()
        tool_total = 0
        tool_errors = 0
        if not sids:
            return [], {"total": 0, "errors": 0, "rate": 0.0}
        placeholders = ",".join("?" * len(sids))
        rows = self.conn.execute(
            f"SELECT role, content, tool_calls FROM messages "
            f"WHERE session_id IN ({placeholders})",
            sids,
        ).fetchall()
        for r in rows:
            if r["tool_calls"]:
                try:
                    for tc in json.loads(r["tool_calls"]):
                        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                        name_counter[fn.get("name", "unknown")] += 1
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
            if r["role"] == "tool":
                tool_total += 1
                content = r["content"] or ""
                try:
                    if isinstance(content, str) and content.strip().startswith("{"):
                        if "error" in json.loads(content):
                            tool_errors += 1
                except (json.JSONDecodeError, ValueError):
                    pass
        rate = (tool_errors / tool_total) if tool_total else 0.0
        return name_counter.most_common(10), {
            "total": tool_total,
            "errors": tool_errors,
            "rate": rate,
        }

    # ── 终端渲染 ────────────────────────────────────────────────────────────
    def format_terminal(self, report: dict) -> str:
        """画框线报表（仿 /sessions / /transport 的 ``[tag]`` 缩进风格）。"""
        ov = report["overview"]
        days = report["days"]
        window = f"last {days}d" if days > 0 else "all time"
        lines = [f"  [insights] {window} — {ov['total_sessions']} sessions"]
        tok = ov["tokens"]
        lines.append(
            f"    tokens: input={tok['input']} output={tok['output']} "
            f"cache_read={tok['cache_read']} cache_write={tok['cache_write']} "
            f"(total={ov['total_tokens']})"
        )
        cost_note = ""
        if ov["unknown_cost_sessions"]:
            cost_note = f"  ({ov['unknown_cost_sessions']} sessions unknown pricing)"
        lines.append(f"    est. cost: ${ov['estimated_cost']:.4f}{cost_note}")
        lines.append(
            f"    turns: total={ov['total_turns']} avg={ov['avg_turns']:.1f}/session"
        )

        fail = report["tool_failure"]
        lines.append(
            f"    tool calls: {fail['total']} results, "
            f"{fail['errors']} errors ({fail['rate']:.1%} failure rate)"
        )

        if report["tool_top"]:
            lines.append("    top tools:")
            for name, count in report["tool_top"]:
                lines.append(f"      {name:<20} {count}")

        if report["per_model"]:
            lines.append("    per model:")
            for r in report["per_model"]:
                lines.append(
                    f"      {r['model']:<20} {r['sessions']}sess  "
                    f"{r['total_tokens']}tok  ${r['cost']:.4f}"
                )
        return "\n".join(lines)
