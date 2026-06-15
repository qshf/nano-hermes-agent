"""V27.4 (C) — delegate 子 agent 继承 MCP 工具。

根因（bug 文档 §8）：connect_mcp_servers 原先跑在 bootstrap 算 parent_full_toolset
快照**之后**，子 agent 的 _resolve_child_toolset 读冻结快照时 mcp_* 还没进 registry
→ 子 agent 永远看不到 MCP 工具（主 agent 读 registry 实时态，不受影响）。本档把
connect 上移到快照之前修掉。

本 suite 验「子允许集真的含 mcp_*」这个语义不变量：
- 单元 1/2：注入含 mcp_* 的父全集后，_resolve_child_toolset 的 None / 白名单交集
  两条路径都能让 mcp_* 进子允许集（mcp_ 不在黑名单）。
- 端到端 3：真连 fake search MCP server，验「快照在 connect 之后算」时 mcp_* 进
  parent_full_toolset → 子允许集 —— 复刻 bootstrap 的正确顺序。
- 回归 4：黑名单（delegate_task / memory / memory_*）仍被子允许集排除，未误伤。

不起真 voice orchestrator：_emit 指向必定连不上的端口（连 fake service 用）。

Run::

    .venv/bin/python scripts/test_v27_4_subagent_mcp.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.delegate_tool import (
    DelegateContext,
    _resolve_child_toolset,
    set_delegate_context,
)
from tools.mcp_client import mcp_manager
from tools.registry import registry

_REPO = Path(__file__).resolve().parent.parent
_FAKE_SEARCH = _REPO / "fake_search_service.py"

# 观测流连不上不影响控制平面（同 test_v27_2 约定）。
os.environ.setdefault("VOICE_ORCHESTRATOR_URL", "http://127.0.0.1:59999/v1/turn-events")
os.environ["VOICE_ORCHESTRATOR_TIMEOUT_SECONDS"] = "0.2"


def _inject(parent_full: set[str]) -> None:
    """注入一个只关心 parent_toolset_names 的轻量 context（chain/model 不参与本测）。"""
    set_delegate_context(DelegateContext(
        chain=None,  # 本 suite 不 spawn 真子 loop，解析工具集不碰 chain
        model="test-model",
        parent_toolset_names=parent_full,
    ))


def test_1_requested_none_includes_mcp_tool():
    """requested=None（子拿父全集 - 黑名单）→ mcp_* 在子允许集里。"""
    _inject({"terminal", "skill_view", "mcp_search_search"})
    allowed = _resolve_child_toolset(None)
    assert "mcp_search_search" in allowed, f"mcp_* 应进子允许集，实得 {allowed}"


def test_2_requested_whitelist_intersects_mcp_tool():
    """requested=[mcp_*] ∩ 父全集 → 拿得到该 MCP 工具。"""
    _inject({"terminal", "mcp_search_search"})
    allowed = _resolve_child_toolset(["mcp_search_search"])
    assert allowed == {"mcp_search_search"}, f"交集应只含被请求且父有的工具，实得 {allowed}"


def test_3_connect_before_snapshot_lets_child_see_mcp():
    """端到端：复刻 bootstrap 正确顺序 —— 先 connect 再算快照，子允许集含 mcp_*。

    这正是本档修的时序：connect_mcp_servers 必须在 parent_full_toolset 之前。
    """
    mcp_manager.connect("search", sys.executable, [str(_FAKE_SEARCH)])
    assert "mcp_search_search" in registry.tool_names, "connect 后 registry 应注册 mcp_*"
    # bootstrap 算快照：registry.tool_names（此刻已含 mcp_*）∪ memory 工具
    parent_full = set(registry.tool_names)
    _inject(parent_full)
    allowed = _resolve_child_toolset(None)
    assert "mcp_search_search" in allowed, (
        "connect 在快照之前 → 子 agent 应继承 mcp_*（这是 C 的核心不变量）"
    )


def test_4_blacklist_still_excludes_delegate_and_memory():
    """回归：mcp_ 解锁不误伤黑名单 —— delegate_task / memory / memory_* 仍被排除。"""
    _inject({"mcp_search_search", "delegate_task", "memory", "memory_recall", "terminal"})
    allowed = _resolve_child_toolset(None)
    assert "mcp_search_search" in allowed
    assert "delegate_task" not in allowed, "delegate_task 必须仍被黑名单挡住（防递归 spawn）"
    assert "memory" not in allowed
    assert "memory_recall" not in allowed, "memory_ 前缀必须仍被排除"


def test_5_provider_reflects_runtime_connect():
    """运行时 /mcp connect：provider 让子全集实时跟父，绕过冻结快照（真正的根因）。

    复刻用户复现的场景 —— agent 跑起来后才连 MCP（工具名 mcp_stdio_*），
    bootstrap 启动期的 parent_toolset_names 快照里没有它。没 provider → 子看不到；
    有 provider → 子当场拿到。
    """
    live: set[str] = {"terminal", "skill_view"}  # 启动期快照（不含运行时工具）
    set_delegate_context(DelegateContext(
        chain=None,
        model="test-model",
        parent_toolset_names=set(live),            # 冻结快照
        parent_toolset_provider=lambda: set(live),  # 实时数据源（指向同一 live）
    ))
    assert "mcp_stdio_search" not in _resolve_child_toolset(["mcp_stdio_search"]), (
        "连接前：子不应看到尚未注册的工具"
    )
    live.add("mcp_stdio_search")  # 模拟运行时 /mcp connect 注册了新工具
    allowed = _resolve_child_toolset(["mcp_stdio_search"])
    assert "mcp_stdio_search" in allowed, (
        "连接后：provider 让子 agent 实时看到运行时新挂的 MCP 工具（绕过冻结快照）"
    )


def test_6_no_provider_falls_back_to_frozen_snapshot():
    """回归：不设 provider → 回落冻结快照（V23.x 行为，~30 处旧测试不受影响）。"""
    set_delegate_context(DelegateContext(
        chain=None,
        model="test-model",
        parent_toolset_names={"read_file"},  # 只有冻结快照，无 provider
    ))
    allowed = _resolve_child_toolset(None)
    assert allowed == {"read_file"}, f"无 provider 应严格用冻结快照，实得 {allowed}"


def _teardown():
    if "search" in mcp_manager.connected_servers:
        mcp_manager.disconnect("search")


def main() -> None:
    tests = [
        test_1_requested_none_includes_mcp_tool,
        test_2_requested_whitelist_intersects_mcp_tool,
        test_3_connect_before_snapshot_lets_child_see_mcp,
        test_4_blacklist_still_excludes_delegate_and_memory,
        test_5_provider_reflects_runtime_connect,
        test_6_no_provider_falls_back_to_frozen_snapshot,
    ]
    failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"✓ {test.__name__}")
            except AssertionError as exc:
                print(f"✗ {test.__name__} — {exc}")
                failed += 1
            except Exception as exc:  # noqa: BLE001
                import traceback
                print(f"✗ {test.__name__} — UNEXPECTED {type(exc).__name__}: {exc}")
                traceback.print_exc()
                failed += 1
    finally:
        _teardown()
    print("=" * 60)
    if failed:
        print(f"  FAIL: {failed}/{len(tests)} 不变量未通过")
        sys.exit(1)
    print(f"  PASS: {len(tests)}/{len(tests)} 不变量")


if __name__ == "__main__":
    main()
