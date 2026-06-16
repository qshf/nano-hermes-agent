"""Fake search service — a standalone MCP server for voice focus-rotation integ.

Two orthogonal planes meet here (see docs multi-service-focus-rotation-plan):
- **Control plane**: this is a FastMCP stdio server exposing one ``search`` tool.
  nano connects to it via mcp_manager and calls ``search`` — a *blocking*
  request/response. During those seconds nano sees no intermediate events.
- **Observation plane**: while ``search`` runs an internal mini-agent loop
  (think → fetch → compose), each beat POSTs a flat v2 envelope straight
  to the voice orchestrator under a stable ``session_id="svc-search"``. Every
  envelope carries ``producer_role="subordinate"`` — declaring "I am an inlaid
  observation stream nested inside the main agent's blocking tool call, not a
  peer turn competing for the speaker." The orchestrator's FocusRouter honors
  this: subordinate streams never own focus, never enter the closing-candidate
  pool (no redundant wrap-up line), and skip the idle gate (spoken immediately,
  no ~30s head blackout). Absent the field, a producer defaults to ``"peer"``
  (a genuinely parallel service like svc-writer keeps the old rotation semantics).

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

# query 参数契约：只接受裸地名（中文 / 英文），不接受带任何修饰词或无意义参数。
# 这些词若出现，说明调用方把意图（"查天气"）塞进了 query —— wttr.in 已默认查天气，
# 地名后面再带 weather / 天气 只会让 URL 变成 wttr.in/Shenzhen%20weather 抓错。
_NOISE_WORDS_EN = {"weather", "forecast", "temperature", "temp", "climate", "report", "today", "tomorrow"}
_NOISE_SUBSTR_CJK = ("天气", "气温", "气候", "预报", "温度", "天氣", "氣溫")
_MAX_WORDS = 4  # "Los Angeles" / "New York City" 合法；再多基本是塞了废话


def _validate_query(query: str) -> str | None:
    """裸地名校验：合格返回 None，不合格返回一句中文 error 说明。

    规则（在发起网络抓取之前跑）：
    1. 去空白后非空；
    2. 只含 Unicode 字母 + 空格 + 连字符 + 撇号（拒 JSON 的 ``{}":,`` 与花括号噪声）；
    3. 不含数字；
    4. 不含「天气 / weather」一类意图修饰词（CJK 子串 + 英文整词两路判）；
    5. 词数 <= 4（再多疑似塞了无意义参数）。
    """
    q = query.strip()
    if not q:
        return "query 不能为空，应为地名，如 'Shenzhen' 或 '广东'"
    for ch in q:
        if ch.isalpha() or ch in " -'":
            continue
        return f"query 只能是中英文地名（字母/空格/连字符），含非法字符 {ch!r}：{query!r}"
    # 上一步已挡掉数字（数字非 isalpha 且不在白名单），此处无需再查 isdigit。
    if any(sub in q for sub in _NOISE_SUBSTR_CJK):
        return f"query 应只含地名，去掉天气/预报等修饰词：{query!r}"
    words = q.split()
    if any(w.lower() in _NOISE_WORDS_EN for w in words):
        return f"query 应只含地名，去掉 weather/forecast 等修饰词：{query!r}"
    if len(words) > _MAX_WORDS:
        return f"query 词数过多（>{_MAX_WORDS}），应为单个地名：{query!r}"
    return None


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
            # V27.4 (A)：自报「我是插入观测流，不是平级的一轮」。主 agent 调 search
            # 是一次阻塞工具调用，这些事件全嵌套在它那一轮之内 —— 不该被 FocusRouter
            # 当成抢麦的平级 producer。orchestrator 认这个标记后：不抢焦点 owner、
            # 不进 closing 候选（不补多余收尾）、不走 idle 闸（开头即时放行）。
            # 缺省 / 不带 = "peer"（真·并行服务如 svc-writer 仍走老语义，向下兼容）。
            "producer_role": "subordinate",
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

    ``query`` MUST be a bare place name in Chinese or English ('New York',
    '广东', 'Shenzhen') — no intent modifiers ('weather', '天气'), no JSON, no
    digits. An invalid query short-circuits to ``{"query", "error"}`` *before*
    any network fetch or voice event, so the caller learns the contract fast.

    Blocks until the fetch finishes (control plane). The voice arc is driven out
    of band by ``_emit`` inside the loop (observation plane).
    """
    err = _validate_query(query)
    if err is not None:
        return json.dumps({"query": query, "error": err}, ensure_ascii=False)
    tid = f"search-{abs(hash(query)) % 100000}"
    _emit("turn_started", turn_id=tid, user_goal=query)
    report = _mini_agent(query, tid)
    _emit("activity_finished", turn_id=tid, user_goal=query,
          activity={"kind": "tool", "name": "web_search", "outcome": "ok"})
    _emit("turn_finished", turn_id=tid, user_goal=query)
    return json.dumps({"query": query, "report": report}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio") # /mcp connect stdio python fake_search_service.py
