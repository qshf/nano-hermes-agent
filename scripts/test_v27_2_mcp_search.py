"""V27.2 (FR-4) — nano mounts the fake search MCP server.

Control plane (MCP request/response) and observation plane (the service POSTs
its own progress envelopes to the voice orchestrator) are separate links. This
suite covers the control plane only: connecting the server registers a tool,
and calling it returns structured JSON. The observation plane is the service's
own concern (it _emits silently; nano never forwards search-internal envelopes).

Run::

    .venv/bin/python scripts/test_v27_2_mcp_search.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.bootstrap import connect_mcp_servers
from tools.mcp_client import mcp_manager
from tools.registry import registry

_REPO = Path(__file__).resolve().parent.parent
_FAKE_SEARCH = _REPO / "fake_search_service.py"

# 联调用：让 _emit 指向一个必定连不上的端口，验证「语音失败绝不影响搜索本职」。
# fake service 的 _emit 在子进程里读这个 env（connect 会把当前环境传给子进程）。
import os
os.environ.setdefault("VOICE_ORCHESTRATOR_URL", "http://127.0.0.1:59999/v1/turn-events")
os.environ["VOICE_ORCHESTRATOR_TIMEOUT_SECONDS"] = "0.2"


def test_1_connect_registers_search_tool():
    mcp_manager.connect("search", sys.executable, [str(_FAKE_SEARCH)])
    assert "search" in mcp_manager.connected_servers
    assert "mcp_search_search" in registry.tool_names


def test_2_call_tool_returns_structured_results():
    # 走 registry dispatch（与主 agent 调工具同一路径）；handler 把结果包成 {"output": ...}
    raw = mcp_manager.call_tool("mcp_search_search", {"query": "焦点轮播测试"})
    payload = json.loads(raw)
    inner = json.loads(payload["output"])   # V21.4 协议：tool_result(output=...)
    assert inner["query"] == "焦点轮播测试"
    assert isinstance(inner["results"], list) and len(inner["results"]) == 6


def test_3_search_unaffected_by_unreachable_orchestrator():
    """观测流（_emit）连不上 orchestrator 时，搜索本职仍正常返回结果。"""
    raw = mcp_manager.call_tool("mcp_search_search", {"query": "断网也要能搜"})
    inner = json.loads(json.loads(raw)["output"])
    assert len(inner["results"]) == 6   # _emit 静默失败，不影响返回


def test_4_bootstrap_helper_parses_env_spec():
    """connect_mcp_servers 解析 NANO_MCP_SERVERS（name=command:arg），坏条目跳过不抛。"""
    import logging
    log = logging.getLogger("test")
    # 坏格式 + 空条目 → 不抛，不连接任何新 server
    before = set(mcp_manager.connected_servers)
    os.environ["NANO_MCP_SERVERS"] = "  ; bad-entry-no-equals ; =empty-name "
    try:
        connect_mcp_servers(log)
    finally:
        os.environ.pop("NANO_MCP_SERVERS", None)
    assert set(mcp_manager.connected_servers) == before


def _teardown():
    if "search" in mcp_manager.connected_servers:
        mcp_manager.disconnect("search")


def main() -> None:
    tests = [
        test_1_connect_registers_search_tool,
        test_2_call_tool_returns_structured_results,
        test_3_search_unaffected_by_unreachable_orchestrator,
        test_4_bootstrap_helper_parses_env_spec,
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
