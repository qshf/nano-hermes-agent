"""Fake search service — a standalone MCP server for voice focus-rotation integ.

Two orthogonal planes meet here (see docs multi-service-focus-rotation-plan):
- **Control plane**: this is a FastMCP stdio server exposing one ``search`` tool.
  nano connects to it via mcp_manager and calls ``search`` — a *blocking*
  request/response. During those seconds nano sees no intermediate events.
- **Observation plane**: while ``search`` runs an internal mini-agent loop
  (think → fetch → compose), each beat POSTs a flat v2 envelope straight
  to the voice orchestrator under a stable ``session_id="svc-search"``. The
  orchestrator's FocusRouter treats this id as one producer and narrates its arc.

The control plane does a real network fetch — ``curl wttr.in/<query>`` — so the
returned text is genuine weather, not canned data. Swapping in another backend
means replacing ``_mini_agent``'s fetch; the envelope-emitting scaffold stays.
``_emit`` failures are swallowed: voice must never affect the search's job.

Run standalone for a smoke test::

    VOICE_ORCHESTRATOR_URL=http://127.0.0.1:8766/v1/turn-events python fake_search_service.py

In integration nano launches it as an MCP subprocess (see agent/bootstrap.py).
Copy this file with ``SESSION="svc-writer"`` + different beats for a 2nd producer.
"""

import json
import os
import subprocess
import urllib.request

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("search")

ORCH = os.environ.get("VOICE_ORCHESTRATOR_URL", "http://127.0.0.1:8766/v1/turn-events")
EMIT_TIMEOUT = float(os.environ.get("VOICE_ORCHESTRATOR_TIMEOUT_SECONDS", "0.5"))
SESSION = "svc-search"  # 独占稳定的 producer 身份（焦点按它区分服务）


def _emit(event_type: str, *, turn_id: str, user_goal: str = "", activity: dict | None = None) -> None:
    """POST one voice-orchestrator.v2 flat envelope; never raise.

    The observation plane is best-effort: a slow/absent orchestrator, a timeout,
    or a connection refusal must not slow down or fail the actual search.
    """
    body = json.dumps(
        {
            "schema_version": "voice-orchestrator.v2",
            "session_id": SESSION,
            "turn_id": turn_id,
            "event_type": event_type,
            "timestamp": 0.0,
            "user_goal": user_goal,
            "activity": activity,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            ORCH, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=EMIT_TIMEOUT).read(0)
    except Exception:
        pass  # 语音失败绝不影响搜索本职


def _mini_agent(query: str, tid: str) -> str:
    """Real weather lookup via ``curl wttr.in``, narrating the arc per beat.

    kind→category in the orchestrator's activity_arc: thinking→CAT_THINK,
    tool/web_search→CAT_SEARCH, generating_text→CAT_COMPOSE — so the spoken arc
    follows 思考 → 联网检索 → 整理回复, audible end-to-end during integ.

    The control plane is now a genuine network fetch (wttr.in), not a sleep; the
    observation plane (``_emit``) is unchanged. A failed/slow fetch still emits
    its beats and returns a readable error — voice never blocks the job.
    """
    _emit("activity_started", turn_id=tid, user_goal=query,
          activity={"kind": "thinking", "name": ""})

    _emit("activity_progress", turn_id=tid, user_goal=query,
          activity={"kind": "tool", "name": "web_search", "completed_count": 0})
    url = f"https://wttr.in/{query}?lang=zh&T"
    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15,
        )
        report = proc.stdout.strip() or f"(wttr.in 无返回, code={proc.returncode})"
    except Exception as exc:  # noqa: BLE001 - 网络失败也要把话说完、把错带回
        report = f"(天气查询失败: {type(exc).__name__}: {exc})"

    _emit("activity_started", turn_id=tid, user_goal=query,
          activity={"kind": "generating_text", "name": ""})  # 汇总
    return report


@mcp.tool()
def search(query: str) -> str:
    """Search by fetching real weather for ``query`` via wttr.in; return as JSON.

    Blocks until the fetch finishes (control plane). The voice arc is driven out
    of band by ``_emit`` inside the loop (observation plane).
    """
    tid = f"search-{abs(hash(query)) % 100000}"
    _emit("turn_started", turn_id=tid, user_goal=query)
    report = _mini_agent(query, tid)
    _emit("activity_finished", turn_id=tid, user_goal=query,
          activity={"kind": "tool", "name": "web_search", "outcome": "ok"})
    _emit("turn_finished", turn_id=tid, user_goal=query)
    return json.dumps({"query": query, "report": report}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio") # /mcp connect stdio python fake_search_service.py
