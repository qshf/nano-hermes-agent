"""Fake search service — a standalone MCP server for voice focus-rotation integ.

Two orthogonal planes meet here (see docs multi-service-focus-rotation-plan):
- **Control plane**: this is a FastMCP stdio server exposing one ``search`` tool.
  nano connects to it via mcp_manager and calls ``search`` — a request/response
  that blocks the *caller* until the fetch returns. During those seconds nano
  sees no intermediate events. ``search`` is ``async`` and offloads its blocking
  arc (wttr.in fetch + ``_emit`` POSTs) to a worker thread, so the FastMCP event
  loop stays free to dispatch a *second* concurrent ``search`` request — two
  delegate child-agents querying different cities now genuinely overlap (N3④),
  instead of running back-to-back. That overlap is what lets the orchestrator's
  FocusRouter rotate between two *live* subordinate streams.
- **Observation plane**: while ``search`` runs an internal mini-agent loop
  (think → fetch → compose), each beat POSTs a flat v2 envelope straight
  to the voice orchestrator. Each call derives a **per-query** ``session_id``
  (``f"svc-search-{qid}"``) so two concurrent ``search`` calls present as two
  distinct subordinate streams — letting the orchestrator's FocusRouter rotate
  focus between them instead of collapsing both into one identity (N3). Every
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
Copy this file with a different ``SESSION_PREFIX`` + beats for a 2nd producer.
"""

import json
import os
import subprocess
import time
import urllib.request

import anyio
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather-search")

ORCH = os.environ.get("VOICE_ORCHESTRATOR_URL", "http://127.0.0.1:8766/v1/turn-events")
EMIT_TIMEOUT = float(os.environ.get("VOICE_ORCHESTRATOR_TIMEOUT_SECONDS", "0.5"))
# N3 修复：身份不再是单一常量。每次 search 调用按 query 派生独立 session_id
# （``f"{SESSION_PREFIX}-{qid}"``），并发的两次查询 = 两条独立的 subordinate 流，
# FocusRouter 才能在它们之间轮播焦点，而非塌缩成同一身份互相串台。
SESSION_PREFIX = "svc-search"

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


def _emit(event_type: str, *, session_id: str, turn_id: str, user_goal: str = "", activity: dict | None = None) -> None:
    """POST one voice-orchestrator.v2 flat envelope; never raise.

    The observation plane is best-effort: a slow/absent orchestrator, a timeout,
    or a connection refusal must not slow down or fail the actual search.

    ``session_id`` is per-query (caller passes ``f"{SESSION_PREFIX}-{qid}"``) so
    concurrent searches are distinct subordinate streams (N3).
    """
    body = json.dumps(
        {
            "schema_version": "voice-orchestrator.v2",
            "session_id": session_id,
            "turn_id": turn_id,
            "event_type": event_type,
            "timestamp": time.time(),  # producer 墙钟；审计可读，但跨 producer 排序仍以 orchestrator 的 logged_at 为权威
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


def _mini_agent(query: str, goal: str, sid: str, tid: str) -> str:
    """Real weather lookup via ``curl wttr.in``, narrating the arc per beat.

    kind→category in the orchestrator's activity_arc: thinking→CAT_THINK,
    tool/web_search→CAT_SEARCH, generating_text→CAT_COMPOSE — so the spoken arc
    follows 思考 → 联网检索 → 整理回复, audible end-to-end during integ.

    ``query`` is the bare place name fed to wttr.in; ``goal`` is the narration
    string fed to the orchestrator (N7：携带"天气"语义，见 ``search``）—— 两者
    刻意分离：URL 抓取只认裸地名，叙事却要让措辞器知道这是「查天气」。

    The control plane is now a genuine network fetch (wttr.in), not a sleep; the
    observation plane (``_emit``) is unchanged. A failed/slow fetch still emits
    its beats and returns a readable error — voice never blocks the job.
    """
    _emit("activity_started", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "thinking", "name": ""})

    _emit("activity_progress", session_id=sid, turn_id=tid, user_goal=goal,
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

    _emit("activity_started", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "generating_text", "name": ""})  # 汇总
    return report


@mcp.tool()
async def search(query: str) -> str:
    """【天气查询，不是通用网页搜索】按地名查实时天气，返回 JSON。

    ⚠️ 与 ``web`` 服务的 ``search`` 区分：那个是**通用网页检索**（关键词 → 网页候选列表，
    再配 ``open_page`` 读正文）。**本工具只做天气**——给一个裸地名，返回该地实时天气，不接受
    通用检索词或主题词。要查网页 / 资料 / 概念时用 ``web`` 服务的 ``search``，别用这个；
    要查某地天气才用这个，别用 ``web`` 服务。

    ``query`` MUST be a bare place name in Chinese or English ('New York',
    '广东', 'Shenzhen') — no intent modifiers ('weather', '天气'), no JSON, no
    digits. An invalid query short-circuits to ``{"query", "error"}`` *before*
    any network fetch or voice event, so the caller learns the contract fast.

    ``async`` on purpose (N3④): validation is inline (pure, no I/O), but the
    blocking arc (wttr.in fetch + best-effort ``_emit`` POSTs) is offloaded to a
    worker thread via ``anyio.to_thread.run_sync``. That keeps the FastMCP event
    loop free to dispatch a *second* concurrent ``search`` request, so two
    delegate child-agents querying different cities overlap in time instead of
    serializing — the precondition for the orchestrator's FocusRouter to rotate
    between two live subordinate streams. The voice arc itself is still driven
    out of band by ``_emit`` inside the (synchronous) ``_search_blocking`` body.
    """
    err = _validate_query(query)
    if err is not None:
        return json.dumps({"query": query, "error": err}, ensure_ascii=False)
    # 阻塞段（网络抓取 + _emit POST）整段进工作线程，event loop 不被占住 → 并发 search 重叠。
    return await anyio.to_thread.run_sync(_search_blocking, query)


def _search_blocking(query: str) -> str:
    """Synchronous arc body — runs inside a worker thread (see ``search``).

    Keeps ``_emit`` / ``_mini_agent`` fully synchronous; isolating the blocking
    work in one thread is what frees the event loop for concurrent calls.
    """
    # 每查询一个身份：qid 同时驱动 session_id 与 turn_id，并发调用互不串台（N3）。
    qid = abs(hash(query)) % 100000
    sid = f"{SESSION_PREFIX}-{qid}"
    tid = f"search-{qid}"
    # N7：query 契约只收裸地名（喂 wttr.in），但"天气"语义只有本服务知道——它就是个
    # 天气服务。叙事字段 user_goal 必须把这层语义带上，否则措辞器拿到光秃秃的"上海"+
    # 阶段"思考"会凭空编主题（实测被说成"上海有什么好玩的/玩法"，旅游）。user_goal 是
    # 自由叙事串、绝不回喂 wttr.in，故可安全携带"天气"而 query 仍保持裸地名。
    goal = f"{query}的天气"
    _emit("turn_started", session_id=sid, turn_id=tid, user_goal=goal)
    report = _mini_agent(query, goal, sid, tid)
    _emit("activity_finished", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "tool", "name": "web_search", "outcome": "ok"})
    _emit("turn_finished", session_id=sid, turn_id=tid, user_goal=goal)
    return json.dumps({"query": query, "report": report}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio") # /mcp connect stdio python fake_search_service.py
