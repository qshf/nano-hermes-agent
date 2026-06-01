"""V25.1 — ``/insights`` + ``/trajectory`` 数据飞轮读路径命令。

- ``/insights [--days N]``：``InsightsEngine(ctx.session_store).generate()`` →
  ``format_terminal`` 出跨会话报表（决策 4：读 v24 SQLite，不读 trajectory jsonl）。
- ``/trajectory list``：列 ``trajectories/`` 下 v25.0 落的样本文件（文件名 / 条数 /
  samples vs failed）。

为什么 insights 读 store 而非 jsonl：见 [agent/insights.py](../../agent/insights.py)
模块 docstring 决策 4 —— SQLite 存原始计量，jsonl 是有损训练格式。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cli.context import AgentCtx
from cli.registry import command, split_args


@command(
    "/insights",
    description="Cross-session stats from v24 SQLite (tokens/cost/tools/failure) — V25.1",
    args_hint="[--days N]",
    category="insights",
)
def cmd_insights(args: str, ctx: AgentCtx) -> None:
    if ctx.session_store is None:
        print("  [insights] no session store (persistence disabled)")
        return
    # 解析 --days N（缺省 30）
    days = 30
    parts = split_args(args)
    if "--days" in parts:
        idx = parts.index("--days")
        if idx + 1 < len(parts):
            try:
                days = int(parts[idx + 1])
            except ValueError:
                print(f"  [insights] invalid --days value: {parts[idx + 1]!r}")
                return

    # 延迟 import：insights 只在用到时才加载（启动期不付成本）
    from agent.insights import InsightsEngine

    engine = InsightsEngine(ctx.session_store)
    report = engine.generate(days=days)
    print(engine.format_terminal(report))


@command(
    "/trajectory",
    description="List trajectory training samples written by V25.0",
    args_hint="list",
    category="insights",
)
def cmd_trajectory(args: str, ctx: AgentCtx) -> None:
    parts = split_args(args)
    sub = parts[0] if parts else "list"
    if sub != "list":
        print(f"  [trajectory] unknown subcommand: {sub} (try: /trajectory list)")
        return

    traj_dir = os.environ.get("TRAJECTORY_DIR", "trajectories")
    if traj_dir == ":none:":
        print("  [trajectory] disabled (TRAJECTORY_DIR=:none:)")
        return
    d = Path(traj_dir)
    if not d.is_dir():
        print(f"  [trajectory] no samples yet ({traj_dir}/ not found)")
        return

    files = sorted(d.glob("*.jsonl"))
    if not files:
        print(f"  [trajectory] no samples yet ({traj_dir}/ empty)")
        return

    print(f"  [trajectory] {len(files)} file(s) in {traj_dir}/")
    for f in files:
        kind = "failed" if f.stem.endswith("_failed") else "samples"
        count = _count_lines(f)
        print(f"    {f.name:<40} {count} convs  [{kind}]")


def _count_lines(path: Path) -> int:
    """数 jsonl 行数（= 落了几个完整对话）。读不动则返回 0，不让 /trajectory 崩。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0
