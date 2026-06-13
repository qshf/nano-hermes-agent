"""Fake search service — a standalone MCP server for voice focus-rotation integ.

Two orthogonal planes meet here (see docs multi-service-focus-rotation-plan):
- **Control plane**: this is a FastMCP stdio server exposing one ``search`` tool.
  nano connects to it via mcp_manager and calls ``search`` — a *blocking*
  request/response. During those seconds nano sees no intermediate events.
- **Observation plane**: while ``search`` runs an internal mini-agent loop
  (think → retrieve ×2 → compose), each beat POSTs a flat v2 envelope straight
  to the voice orchestrator under a stable ``session_id="svc-search"``. The
  orchestrator's FocusRouter treats this id as one producer and narrates its arc.

No real search engine, no external API — the mini-agent just sleeps between
beats. Swapping in a real agent means replacing ``_mini_agent``'s body; the
envelope-emitting scaffold stays. ``_emit`` failures are swallowed: voice must
never affect the search's actual job.

Run standalone for a smoke test::

    VOICE_ORCHESTRATOR_URL=http://127.0.0.1:8766/v1/turn-events python fake_search_service.py

In integration nano launches it as an MCP subprocess (see agent/bootstrap.py).
Copy this file with ``SESSION="svc-writer"`` + different beats for a 2nd producer.
"""

import json
import os
import time
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


def _mini_agent(query: str, tid: str) -> list[str]:
    """Minimal agent loop: think → retrieve ×2 → compose, emitting per beat.

    kind→category in the orchestrator's activity_arc: thinking→CAT_THINK,
    tool/web_search→CAT_SEARCH, generating_text→CAT_COMPOSE — so the spoken arc
    follows 思考 → 联网检索 → 整理回复, audible end-to-end during integ.
    """
    _emit("activity_started", turn_id=tid, user_goal=query,
          activity={"kind": "thinking", "name": ""})
    time.sleep(1.0)

    hits: list[str] = []
    for round_i in range(2):
        _emit("activity_progress", turn_id=tid, user_goal=query,
              activity={"kind": "tool", "name": "web_search", "completed_count": round_i})
        time.sleep(1.5)
        hits += [f"result-{round_i}-{j}" for j in range(3)]

    _emit("activity_started", turn_id=tid, user_goal=query,
          activity={"kind": "generating_text", "name": ""})  # 汇总
    time.sleep(0.8)
    return hits


@mcp.tool()
def search(query: str) -> str:
    """Fake search: run a mini-agent loop and return ranked results as JSON.

    Blocks until the loop finishes (control plane). The voice arc is driven out
    of band by ``_emit`` inside the loop (observation plane).
    """
    tid = f"search-{abs(hash(query)) % 100000}"
    _emit("turn_started", turn_id=tid, user_goal=query)
    hits = _mini_agent(query, tid)
    _emit("activity_finished", turn_id=tid, user_goal=query,
          activity={"kind": "tool", "name": "web_search", "outcome": "ok",
                    "completed_count": len(hits)})
    _emit("turn_finished", turn_id=tid, user_goal=query)
    return json.dumps({"query": query, "results": hits}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
