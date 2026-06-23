"""Fake web service — a standalone MCP server that mocks web search + browsing.

Sibling to ``fake_search_service.py`` (the weather backend). Like it, this file
spans **two orthogonal planes** (see docs multi-service-focus-rotation-plan):

- **Control plane**: a FastMCP stdio server exposing ``search`` + ``open_page``.
  Page bodies are mocked by reading local fixture files via a URL → filename
  mapping (``_PAGES``); no network is touched. ``search`` keyword-matches the
  query against each page's title + keywords + cached body and returns the top
  hits; ``open_page`` resolves a URL and returns its full body.
- **Observation plane**: each tool runs a tiny narrated arc (think → search /
  open → compose) and POSTs a flat v2 envelope to the voice orchestrator per
  beat — the same ``_emit`` scaffold fake_search uses. Every envelope carries
  ``producer_role="subordinate"`` ("I am an inlaid observation stream nested
  inside the main agent's blocking tool call, not a peer turn"). Each call
  derives a **per-call** ``session_id`` (``f"{SESSION_PREFIX}-{qid}"``) so two
  concurrent calls present as two distinct subordinate streams the orchestrator's
  FocusRouter can rotate between, instead of collapsing into one identity (N3).

Why this service narrates with deliberate **pacing** (``_BEAT_PAUSE`` sleeps),
unlike fake_search: fake_search's beats spread out for free because ``wttr.in``
takes seconds, so its arc is audible. fake_web's fixture reads finish in
microseconds — without pacing every beat fires inside the same ~2s subordinate
floor window and all but the opener get suppressed (the latent N5/floor pattern,
nothing to hear). The sleeps (each > the orchestrator's subordinate floor) space
the beats so each phase transition clears the floor and is actually spoken, and
so two concurrent ``search`` calls live long enough to overlap for focus
rotation. The arc is also ordered **forward** (think → search → compose; the
tool-``finished`` beat lands same-category, naturally ``not_salient``) so it
never replays "开始检索" after "整理回复" the way fake_search's emit order would
if it weren't floor-suppressed (N5).

The sleeps run inside a worker thread: both tools are ``async`` and offload their
blocking arc via ``anyio.to_thread.run_sync``, keeping the FastMCP event loop
free to dispatch a *second* concurrent call — the precondition for two live
subordinate streams (N3④). ``_emit`` failures are swallowed: voice must never
affect the tool's job.

Path safety: ``open_page`` never joins caller input to a path. The URL is only
ever *looked up* in ``_PAGES``; an unknown URL short-circuits to an error. The
resolved fixture path is additionally asserted to live inside the fixtures dir,
so a malformed mapping entry can't read outside the sandbox.

Run standalone for a smoke test::

    VOICE_ORCHESTRATOR_URL=http://127.0.0.1:8766/v1/turn-events python fake_web_service.py

In integration nano launches it as an MCP subprocess via ``NANO_MCP_SERVERS``::

    NANO_MCP_SERVERS=web=python:/abs/path/fake_web_service.py
"""

import json
import os
import time
import urllib.request
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web")

ORCH = os.environ.get("VOICE_ORCHESTRATOR_URL", "http://127.0.0.1:8766/v1/turn-events")
EMIT_TIMEOUT = float(os.environ.get("VOICE_ORCHESTRATOR_TIMEOUT_SECONDS", "0.5"))
# N3：身份按调用派生独立 session_id（``f"{SESSION_PREFIX}-{qid}"``），并发的两次调用 =
# 两条独立 subordinate 流，FocusRouter 才能在它们之间轮播焦点，而非塌缩成同一身份。
SESSION_PREFIX = "svc-web"

# 节奏：本地 fixture 读取是微秒级，beat 若挤在一起会全被 subordinate floor（~2s）压掉，
# 只剩开场可听（潜伏 N5 模式）。故每个软帧之间停 _BEAT_PAUSE（> floor）让阶段切换清过
# floor 真正播出；收尾前停 _TAIL_PAUSE 让最后一句被听到。sleep 跑在工作线程里（见 search
# 的 anyio.to_thread），不占 event loop，故两次并发调用仍能时间上重叠供轮播。
_BEAT_PAUSE = 2.5
_TAIL_PAUSE = 1.5

# 网页正文 fixtures 根目录；open_page 只在此目录内读文件（路径沙箱）。
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "web_pages"

# URL → 网页元数据的映射，正文不内联、由 file 指向 fixtures 下的纯文本。
# keywords 仅用于 search 召回加权（标题/正文之外的同义词补充）；正文匹配也参与召回。
_PAGES: dict[str, dict] = {
    "https://example.com/python": {
        "title": "Python 入门指南",
        "file": "python-intro.txt",
        "keywords": ["python", "编程", "脚本", "解释型", "入门"],
    },
    "https://example.com/mcp": {
        "title": "Model Context Protocol（MCP）概览",
        "file": "mcp-spec.txt",
        "keywords": ["mcp", "协议", "工具", "tool", "大模型", "server"],
    },
    "https://example.com/weather-faq": {
        "title": "天气查询常见问题",
        "file": "weather-faq.txt",
        "keywords": ["天气", "weather", "温度", "wttr", "预报", "城市"],
    },
    "https://example.com/nano-hermes": {
        "title": "nano_hermes_agent 项目简介",
        "file": "nano-hermes.txt",
        "keywords": ["agent", "智能体", "记忆", "教学", "语音", "nano"],
    },
}

_MAX_RESULTS = 5  # search 最多返回的候选数
_SNIPPET_CHARS = 80  # 每条结果摘要截断长度


def _emit(event_type: str, *, session_id: str, turn_id: str, user_goal: str = "", activity: dict | None = None) -> None:
    """POST one voice-orchestrator.v2 flat envelope; never raise.

    The observation plane is best-effort: a slow/absent orchestrator, a timeout,
    or a connection refusal must not slow down or fail the actual tool. Identical
    in shape to fake_search's ``_emit`` (same schema/role/timeout semantics).

    ``session_id`` is per-call (caller passes ``f"{SESSION_PREFIX}-{qid}"``) so
    concurrent calls are distinct subordinate streams (N3).
    """
    body = json.dumps(
        {
            "schema_version": "voice-orchestrator.v2",
            "session_id": session_id,
            "turn_id": turn_id,
            "event_type": event_type,
            "timestamp": time.time(),  # producer 墙钟；跨 producer 排序仍以 orchestrator 的 logged_at 为权威
            "user_goal": user_goal,
            "activity": activity,
            # 自报「我是插入观测流，不是平级的一轮」：主 agent 调本工具是一次阻塞调用，
            # 这些事件全嵌套在它那一轮之内。orchestrator 认这个标记后：不抢焦点 owner、
            # 不进 closing 候选（不补多余收尾）、不走 idle 闸。缺省 = "peer"（向下兼容）。
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
        pass  # 语音失败绝不影响工具本职


def _read_body(url: str) -> str:
    """读取 ``url`` 映射到的 fixture 正文；越界 / 缺失抛 ValueError。

    ``_PAGES[url]["file"]`` 是我们自管的相对文件名，但仍做沙箱断言：解析后的
    绝对路径必须落在 ``_FIXTURES`` 之内，避免坏映射条目（如 ``../secret``）越权读。
    """
    meta = _PAGES.get(url)
    if meta is None:
        raise ValueError(f"未收录的 URL：{url!r}")
    path = (_FIXTURES / meta["file"]).resolve()
    if not path.is_relative_to(_FIXTURES):
        raise ValueError(f"映射文件越出沙箱：{meta['file']!r}")
    if not path.is_file():
        raise ValueError(f"网页正文缺失：{meta['file']!r}")
    return path.read_text(encoding="utf-8")


def _score(query: str, url: str, meta: dict) -> int:
    """对单个网页打召回分：标题命中权重最高，其次关键词，再次正文。

    **按词打分**：query 先按空白拆成词（去重），每个词独立子串匹配标题/关键词/正文，
    分数累加。这样多词 query（'MCP Python' / 'Model Context Protocol Python SDK'）也能
    命中——否则把整条 query 当一个子串去比，'mcp python' 永远配不上单词关键词 'mcp'，
    多词检索一律 0 命中（实测 fake_web 多词查询全空、agent 误报"没找到"的根因）。
    全部小写后做子串匹配（中文无大小写，英文大小写无关）。0 分表示不相关。
    正文读取失败不应让整个 search 崩 —— 此页正文按缺失（不加分）处理。
    """
    tokens = list(dict.fromkeys(w for w in query.strip().lower().split() if w))
    if not tokens:
        return 0
    title = meta["title"].lower()
    keywords = [kw.lower() for kw in meta["keywords"]]
    try:
        body = _read_body(url).lower()
    except ValueError:
        body = ""
    score = 0
    for tok in tokens:
        if tok in title:
            score += 5
        if any(tok in kw for kw in keywords):
            score += 3
        if body and tok in body:
            score += 1
    return score


@mcp.tool()
async def search(query: str) -> str:
    """【通用网页搜索，不是天气查询】按关键词召回候选网页，返回结果列表 JSON。

    ⚠️ 与 ``search`` 服务的 ``search`` 区分：那个**只查天气**（给裸地名，返回某地实时天气）。
    本工具是**通用网页检索**——给主题关键词（'python' / 'mcp' / '天气查询' 等），拿到候选
    网页，再用 ``open_page(url)`` 读正文。要查某地实时天气请用天气服务，别用这个。

    对 ``_PAGES`` 中每页用标题/关键词/正文做**按词**子串打分（query 按空白分词，逐词累加），
    取分数 > 0 的前若干条，每条形如 ``{"url", "title", "snippet"}``（snippet 为正文前若干字摘要）。
    无命中返回 ``{"query", "results": []}`` —— 调用方据此再决定 open_page 哪条。

    ``async``（N3④）：纯打分本身极快，但叙事弧线带 ``_BEAT_PAUSE`` 节奏 sleep，
    整段（含 ``_emit`` POST + sleep）offload 进工作线程，event loop 不被占住 →
    两次并发 search 时间上重叠，FocusRouter 可在两条 live 子流间轮播。
    """
    return await anyio.to_thread.run_sync(_search_blocking, query)


def _search_blocking(query: str) -> str:
    """Synchronous narrated arc for ``search`` — runs in a worker thread.

    Forward arc 思考 → 联网检索 → 整理回复：每个软帧前停 ``_BEAT_PAUSE`` 越过
    subordinate floor 才播得出。web_search 的 ``activity_finished`` 与其
    ``activity_progress`` 同类别（CAT_SEARCH），落在 compose 之前，故自然
    ``not_salient`` 被静默消化，不会在「整理回复」后倒退冒「开始检索」（N5）。
    """
    qid = abs(hash(query)) % 100000
    sid = f"{SESSION_PREFIX}-{qid}"
    tid = f"web-search-{qid}"
    # N7：user_goal 是自由叙事串（绝不回喂打分逻辑），携带「搜索网页」语义，否则措辞器
    # 拿到光秃秃的 query + 阶段「思考」会凭空编主题。
    goal = f"搜索{query}的网页"

    _emit("turn_started", session_id=sid, turn_id=tid, user_goal=goal)
    _emit("activity_started", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "thinking", "name": ""})
    time.sleep(_BEAT_PAUSE)

    _emit("activity_progress", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "tool", "name": "web_search", "completed_count": 0})
    # 实际召回打分（极快）
    scored = []
    for url, meta in _PAGES.items():
        s = _score(query, url, meta)
        if s > 0:
            scored.append((s, url, meta))
    scored.sort(key=lambda t: t[0], reverse=True)
    results = []
    for _, url, meta in scored[:_MAX_RESULTS]:
        try:
            snippet = _read_body(url).strip().replace("\n", " ")[:_SNIPPET_CHARS]
        except ValueError:
            snippet = ""
        results.append({"url": url, "title": meta["title"], "snippet": snippet})
    payload = json.dumps({"query": query, "results": results}, ensure_ascii=False)
    time.sleep(_BEAT_PAUSE)

    # finished 与上面的 web_search progress 同类别 → not_salient（静默），避免 N5 倒序。
    _emit("activity_finished", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "tool", "name": "web_search", "outcome": "ok"})
    _emit("activity_started", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "generating_text", "name": ""})  # 整理结果列表
    time.sleep(_TAIL_PAUSE)

    _emit("turn_finished", session_id=sid, turn_id=tid, user_goal=goal)
    return payload


@mcp.tool()
async def open_page(url: str) -> str:
    """打开一个网页（``web`` 服务）：按 ``url`` 查 fixture 文件并返回正文 JSON。

    与 ``search`` 配对：先用本服务的 ``search`` 拿候选 url，再 ``open_page`` 取正文。
    ``url`` 必须是 ``search`` 返回过的网址（``https://example.com/...``），不是地名、不是
    搜索词——查天气 / 关键词检索都不走这里。

    命中返回 ``{"url", "title", "content"}``（content 为整篇正文）；未收录或
    正文缺失/越界返回 ``{"url", "error"}``。

    ``async``（同 ``search``）：叙事弧线带节奏 sleep，整段 offload 进工作线程，
    不占 event loop，故与并发的 search / open_page 重叠供 FocusRouter 轮播。
    """
    return await anyio.to_thread.run_sync(_open_page_blocking, url)


def _open_page_blocking(url: str) -> str:
    """Synchronous narrated arc for ``open_page`` — runs in a worker thread.

    弧线 思考 → 打开网页（web_fetch）。读取失败时 finished 带 ``outcome="error"``
    → 措辞器走 REASON_ERROR 安抚一句，演示错误路径；成功时 finished 同类别静默。
    保留原有错误语义：未收录 / 越界 / 缺失均返回 ``{"url", "error"}``。
    """
    qid = abs(hash(url)) % 100000
    sid = f"{SESSION_PREFIX}-{qid}"
    tid = f"web-open-{qid}"
    # N7：携带真实语义。命中页带上标题，否则退到 url，措辞器据此说「在打开 X」而非编造。
    title = (_PAGES.get(url) or {}).get("title", "")
    goal = f"打开网页《{title}》" if title else f"打开网页 {url}"

    _emit("turn_started", session_id=sid, turn_id=tid, user_goal=goal)
    _emit("activity_started", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "thinking", "name": ""})
    time.sleep(_BEAT_PAUSE)

    _emit("activity_progress", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "tool", "name": "web_fetch", "completed_count": 0})
    try:
        content = _read_body(url)
    except ValueError as exc:
        time.sleep(_TAIL_PAUSE)
        _emit("activity_finished", session_id=sid, turn_id=tid, user_goal=goal,
              activity={"kind": "tool", "name": "web_fetch", "outcome": "error"})
        _emit("turn_finished", session_id=sid, turn_id=tid, user_goal=goal)
        return json.dumps({"url": url, "error": str(exc)}, ensure_ascii=False)
    time.sleep(_TAIL_PAUSE)

    _emit("activity_finished", session_id=sid, turn_id=tid, user_goal=goal,
          activity={"kind": "tool", "name": "web_fetch", "outcome": "ok"})
    _emit("turn_finished", session_id=sid, turn_id=tid, user_goal=goal)
    return json.dumps(
        {"url": url, "title": _PAGES[url]["title"], "content": content},
        ensure_ascii=False,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")  # /mcp connect stdio python fake_web_service.py
