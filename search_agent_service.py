"""Search agent service — an MCP server whose single tool *is* a sub-agent.

教学定位（v27.5 · MCP-as-Agent / 自洽子 Agent 服务）
====================================================
nano-hermes-agent 走常规 MCP 机制调本服务，但本服务背后不是一段写死的脚本，
而是一个**独立进程里跑 LLM 循环的子 Agent**：它自己决定搜什么、开哪页、怎么归纳，
把真实推理进展作为 subordinate 观测流播给语音编排器，最后只把综合答案回吐给 nano。

与初版三进程方案的区别（决策见 docs/decisions/v27.5.md）
--------------------------------------------------------
初版曾让本服务**再去当 fake_web 的 MCP client**（第三进程 + 第二条 event loop +
双观测流静音 hack）。但「agent 又是个 MCP client」不在目标内（那课 v27.2 已单独讲过），
故砍掉：本服务把 fake_web 的检索/读 fixture 逻辑当**普通 Python 模块 import 复用**
（``_score`` / ``_read_body`` / ``_PAGES``），工具就在手边、无 MCP 跳。两进程、低耦合、
单文件可移植——别的项目照这个模式塞自己的工具即可。

两个正交平面（沿用 fake_web 的双平面模型）
------------------------------------------
- **控制平面**：FastMCP stdio server 暴露**一个**高层工具 ``research(task)``
  （nano 侧见 ``mcp_researcher_research``）。内部跑 ``run_child_loop``，子 Agent 调
  本进程 registry 里的两个**本地、静默**工具 ``search`` / ``open_page``。
- **观测平面**：子 Agent 真实推理经 ``progress_callback`` 转成 subordinate 中间播报
  （模型决定调工具的时点 → "正在检索网页 / 正在打开网页"）。每次 ``research`` 派生
  per-call ``session_id``（N3：并发两次 = 两条独立子流供 FocusRouter 轮播）；
  ``user_goal`` 携带语义（N7：措辞器不空编）。``_emit`` 失败全吞（语音绝不影响本职）。

承重前提
--------
- **自读 .env**：MCP SDK spawn 子进程时只继承安全白名单（HOME/PATH/SHELL/…），
  ``OPENAI_*`` / ``MODEL`` / ``STREAM_ENABLED`` **不透传**。故本服务启动即
  ``load_dotenv`` 自给自足拿到 LLM 配置——nano 一行不动。
- **真叙事依赖 ``STREAM_ENABLED=1``**：开流式才有逐工具事件（已确认 chat_completions /
  anthropic 流式路径均带 ``EVENT_TOOL_CALL_STARTED`` + ``tool_name``）。否则
  ``run_child_loop`` 走同步退化路径、无逐工具事件 → 叙事塌成循环前补的一句通用播报。

Run standalone for a smoke test（建议用绝对 venv python 起，确保拿到依赖）::

    VOICE_ORCHESTRATOR_URL=http://127.0.0.1:8766/v1/turn-events \
      /abs/.venv/bin/python search_agent_service.py

In integration nano launches it as an MCP subprocess via ``NANO_MCP_SERVERS``::

    NANO_MCP_SERVERS=researcher=/abs/.venv/bin/python:/abs/search_agent_service.py
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

# 自读 .env：本服务作为 MCP 子进程被 spawn 时，父环境只透传安全白名单（不含
# OPENAI_*/MODEL/STREAM_ENABLED）。在任何 transport import 之前补齐 env，让
# build_chain_from_env 能拿到 key——这一步是「nano 零改动」的关键。
try:  # pragma: no cover - 环境相关
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:  # noqa: BLE001 - dotenv 缺失/读不到不致命，env 已在也能跑
    pass

import anyio
from mcp.server.fastmcp import FastMCP

# 复用 fake_web 的检索/读 fixture 逻辑当普通模块（不经 MCP）。import 仅定义、无网络副作用。
import fake_web_service as web
from agent.child_loop import run_child_loop
from tools.registry import ToolRegistry
from tools.result import tool_result
from transports.chain import build_chain_from_env
from transports.client_factory import make_llm_client
from transports.streaming import (
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL_STARTED,
    StreamEvent,
)

mcp = FastMCP("researcher")

# 观测平面配置（与 fake_web 同语义；ORCH="" 视为静音）。
ORCH = os.environ.get("VOICE_ORCHESTRATOR_URL", "http://127.0.0.1:8766/v1/turn-events")
EMIT_TIMEOUT = float(os.environ.get("VOICE_ORCHESTRATOR_TIMEOUT_SECONDS", "0.5"))
SESSION_PREFIX = "svc-researcher"

# 审计落盘 tap（env ``SEARCH_AGENT_LOG`` 给路径才开）：本服务是 MCP **stdio** server，
# stdout 被协议占用、不能 print 调试；envelope 又只是 best-effort POST 给编排器，编排器
# 没起 / 没存就彻底丢失，无法独立审核服务究竟发了什么。故在此加一条本地 JSONL：每条
# envelope（及 research 调用边界）落盘一行，**与是否真 POST 解耦**——哪怕静音 / 编排器
# 离线也照样存。相对路径按服务文件目录解析（子进程 CWD 不定）。
_LOG_PATH = os.environ.get("SEARCH_AGENT_LOG", "").strip()
if _LOG_PATH and not os.path.isabs(_LOG_PATH):
    _LOG_PATH = str(Path(__file__).resolve().parent / _LOG_PATH)
_LOG_LOCK = threading.Lock()  # 并发 research 在各自工作线程里追加同一文件，串行化写

# 子 Agent 只许用这两个本地工具。
_ALLOWED_TOOLS = {"search", "open_page"}

# 语音桥参数：reasoning_hint 截断长度 + 长流（思考/compose）期每攒够多少字补发一拍。
# 这两个数只控**节奏**，不含任何工具/措辞内容——叙事素材全部取自 agent 自己的流。
# _FLUSH 取小（60）：中文一段就上百字，阈值太大会让中短答案整段不发拍（compose 静默→
# 编排器只能补"马上就好"）。发多了无妨——编排器 subordinate floor 自会节流，多余拍被丢。
_HINT_MAX_CHARS = 200
_FLUSH_EVERY_CHARS = 60
_BUF_KEEP_CHARS = 600  # 思路缓冲只留尾部这么多，避免无界增长

# chain 惰性构建并缓存：避免 import 期强依赖 key（测试可 monkeypatch 这两个全局）。
_CHAIN = None
_MODEL = None


def _env_bool(key: str, default: bool) -> bool:
    """轻量 env 布尔解析（不引 agent.env，保持本服务自洽）。"""
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _ensure_chain() -> tuple:
    """首次 research 时建 TransportChain + 解析默认 model，之后复用。"""
    global _CHAIN, _MODEL
    if _CHAIN is None:
        chain_env = os.environ.get("TRANSPORT_CHAIN") or os.environ.get(
            "TRANSPORT_MODE", "chat_completions"
        )
        _CHAIN = build_chain_from_env(chain_env, client_factory=make_llm_client)
        _MODEL = _CHAIN.primary_model or os.environ.get("MODEL", "gpt-4o-mini")
    return _CHAIN, _MODEL


# ─── 子 Agent 的本地工具：静默版 search / open_page（叙事交给 LLM 流，不在工具里发声）──


def _tool_search(args: dict) -> str:
    """通用网页检索：复用 fake_web 的按词打分，返回候选列表 JSON。"""
    query = args.get("query", "") or ""
    scored = []
    for url, meta in web._PAGES.items():
        s = web._score(query, url, meta)
        if s > 0:
            scored.append((s, url, meta))
    scored.sort(key=lambda t: t[0], reverse=True)
    results = []
    for _, url, meta in scored[: web._MAX_RESULTS]:
        try:
            snippet = web._read_body(url).strip().replace("\n", " ")[: web._SNIPPET_CHARS]
        except ValueError:
            snippet = ""
        results.append({"url": url, "title": meta["title"], "snippet": snippet})
    return tool_result({"query": query, "results": results})


def _tool_open_page(args: dict) -> str:
    """打开网页：复用 fake_web 的沙箱读取，返回正文 JSON（未收录/越界回 error）。"""
    url = args.get("url", "") or ""
    try:
        content = web._read_body(url)
    except ValueError as exc:
        return tool_result({"url": url, "error": str(exc)})
    return tool_result({"url": url, "title": web._PAGES[url]["title"], "content": content})


# 本进程自建 registry（非全局单例）：子 Agent 的工具面就这两个。
_REGISTRY = ToolRegistry()
_REGISTRY.register(
    {
        "name": "search",
        "description": (
            "【通用网页搜索，不是天气查询】给主题关键词（'python' / 'mcp' / '天气查询' 等），"
            "返回候选网页列表 JSON（每条 {url,title,snippet}）。再用 open_page(url) 读正文。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    },
    _tool_search,
)
_REGISTRY.register(
    {
        "name": "open_page",
        "description": (
            "打开一个网页：传 search 返回过的 url（https://example.com/...），返回整篇正文 JSON。"
            "未收录/越界返回 {url,error}。查关键词请先用 search。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "search 返回的网址"}},
            "required": ["url"],
        },
    },
    _tool_open_page,
)


# ─── 观测平面：subordinate envelope + 语音桥 ────────────────────────────────


def _record(kind: str, payload: dict) -> None:
    """把一条审计记录追加到本地 JSONL（``SEARCH_AGENT_LOG`` 设了才写）；绝不抛。

    与 POST 解耦：无论编排器在不在、是否静音，都落盘。``recorded_at`` 是 producer
    墙钟（仅审计排序用）。审计写失败也吞掉——绝不影响工具本职。
    """
    if not _LOG_PATH:
        return
    try:
        rec = {"recorded_at": round(time.time(), 3), "kind": kind, **payload}
        line = json.dumps(rec, ensure_ascii=False)
        with _LOG_LOCK:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _emit(
    event_type: str,
    *,
    session_id: str,
    turn_id: str,
    user_goal: str = "",
    activity: dict | None = None,
    reasoning_hint: str = "",
) -> None:
    """POST 一条 voice-orchestrator.v2 flat envelope；绝不抛。

    与 fake_web 的 ``_emit`` 同形（schema / role / timeout 语义一致）。``ORCH`` 为空
    字符串（被显式传 ``VOICE_ORCHESTRATOR_URL=""`` 或未配且默认被改空）时直接静音。

    ``reasoning_hint`` 是 agent **自己的思路原文片段**（取自流式 reasoning/text delta），
    编排器措辞器据此说人话（``phrase_planner`` 的「思路摘要」槽）。本服务只转发事实，
    不替它措辞——这就是「不写死、可通用」：换任何工具/项目，料都来自 agent 的流。

    审计落盘在 ORCH 静音判断**之前**——即使不投递也要能审核服务发了什么。
    """
    payload = {
        "schema_version": "voice-orchestrator.v2",
        "session_id": session_id,
        "turn_id": turn_id,
        "event_type": event_type,
        "user_goal": user_goal,
        "activity": activity,
        "reasoning_hint": reasoning_hint,
        # 自报「我是插入观测流，不是平级的一轮」：nano 调 research 是一次阻塞调用，
        # 这些事件全嵌套在它那一轮内。orchestrator 据此不抢焦点 owner / 不补收尾。
        "producer_role": "subordinate",
    }
    _record("envelope", payload)  # 解耦：先落盘审计，再决定是否投递
    if not ORCH:
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            ORCH, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=EMIT_TIMEOUT).read(0)
    except Exception:  # noqa: BLE001
        pass  # 语音失败绝不影响工具本职


# 句子边界符（中英文标点 + 换行）。markdown 代码/表格里换行频繁，足以切出完整片段。
_SENTENCE_SEPS = ("。", "！", "？", "；", "\n", ". ", "! ", "? ", "; ")


def _split_complete(text: str) -> tuple[str, str]:
    """把累积文本切成 (完整片段, 余下半句)：在**最后一个**句子边界处切。

    返回的"完整片段"以句子结尾收口；"余下半句"是边界之后还没说完的部分，由调用方
    进位到下一拍，与后续 delta 拼接——这样每拍 reasoning_hint 都是整句起、整句止，
    根治"从句中起头"（实测 compose 补发每段 ~60 字、永远不触发旧的事后裁剪）。

    没有任何边界（罕见：一长串无标点）→ 返回 ("", text)，让调用方继续攒，
    除非已超 ``_BUF_KEEP_CHARS`` 安全阀（那时强制整段发出，避免无限饿死）。
    """
    last = -1
    for sep in _SENTENCE_SEPS:
        idx = text.rfind(sep)
        if idx != -1:
            last = max(last, idx + len(sep))
    if last == -1:
        if len(text) > _BUF_KEEP_CHARS:  # 安全阀：太长无边界，强制发出
            return text, ""
        return "", text  # 还没凑出一个整句，继续攒
    return text[:last], text[last:]


def _clean_hint(raw: str) -> str:
    """把一段文本整理成 reasoning_hint：限长（取末 ``_HINT_MAX_CHARS`` 字）+ 对齐句首。

    超长时取尾部最新鲜的一段，再丢掉开头被切出的半句残文（对齐到第一个句子边界之后），
    避免"传输方式选择：-"这种断头话。未超长则原样返回（短片段不强切）。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) <= _HINT_MAX_CHARS:
        return s
    s = s[-_HINT_MAX_CHARS:]
    best = -1
    for sep in _SENTENCE_SEPS:
        idx = s.find(sep)
        if idx != -1:
            cut = idx + len(sep)
            best = cut if best == -1 else min(best, cut)
    cleaned = s[best:].strip() if best != -1 else s
    return cleaned or s



def _make_voice_cb(session_id: str, turn_id: str, user_goal: str):
    """造一个 progress_callback：把子 Agent 的流转成中间播报。

    **关键区分（reasoning vs 回复正文）**——这俩语义完全不同，绝不能混进同一个 hint：
    - ``reasoning_delta`` = agent 的**思考**（"我先查 MCP 定义，再打开页面归纳"）。这才是
      ``reasoning_hint`` 该装的东西：措辞器（``phrase_planner`` 的「思路摘要」槽）据此说人话。
      累积、按句子边界进位（``_split_complete`` + ``_clean_hint``），整句起整句止。
    - ``text_delta`` = agent 写给用户的**回复正文**（"## 一、MCP 协议概述…"）。这是答案、不是
      思路，**绝不进 reasoning_hint**（否则措辞器会去复读你的答案）。compose 阶段只发节奏拍
      （``activity.kind=generating_text`` 让编排器知道"还在写、别催"），``reasoning_hint`` 留空——
      措辞器拿空 hint + 该 kind 会说"正在整理回复"，而不是念正文。

    设计原则不变：不在服务里手写任何话术；流里取不到工具入参（transport 只暴露长度），
    故思路素材一律取 agent 自己的 reasoning 文本。模型这拍没吐 reasoning → hint 空 → 优雅退回。
    """
    buf: list[str] = []  # 仅 **reasoning** 累积（只留尾部 _BUF_KEEP_CHARS）—— 给 tool 拍取"刚才在想什么"
    pending = [""]  # 自上次 reasoning 发拍以来、尚未发出的思路（含上拍进位的半句）
    rsince = [0]  # 距上次 reasoning 补发的新增思路字数
    tsince = [0]  # 距上次 compose 节奏拍的新增正文字数

    def cb(ev: StreamEvent) -> None:
        if ev.type == EVENT_REASONING_DELTA and ev.text:
            # 真思路：累积进 buf/pending，攒够整句就补发一拍（带 reasoning_hint）。
            buf.append(ev.text)
            pending[0] += ev.text
            joined = "".join(buf)
            if len(joined) > _BUF_KEEP_CHARS:
                buf.clear()
                buf.append(joined[-_BUF_KEEP_CHARS:])
            rsince[0] += len(ev.text)
            if rsince[0] >= _FLUSH_EVERY_CHARS:
                complete, remainder = _split_complete(pending[0])
                if complete.strip():
                    _emit(
                        "activity_progress",
                        session_id=session_id,
                        turn_id=turn_id,
                        user_goal=user_goal,
                        activity={"kind": "thinking", "name": ""},
                        reasoning_hint=_clean_hint(complete),
                    )
                    pending[0] = remainder
                    rsince[0] = 0
        elif ev.type == EVENT_TEXT_DELTA and ev.text:
            # 回复正文：**不**进 reasoning_hint。只发节奏拍让编排器知道"还在写"，hint 留空。
            tsince[0] += len(ev.text)
            if tsince[0] >= _FLUSH_EVERY_CHARS:
                _emit(
                    "activity_progress",
                    session_id=session_id,
                    turn_id=turn_id,
                    user_goal=user_goal,
                    activity={"kind": "generating_text", "name": ""},
                    reasoning_hint="",  # 答案正文不当思路转发
                )
                tsince[0] = 0
        elif ev.type == EVENT_TOOL_CALL_STARTED and ev.tool_name:
            # agent 决定调工具：带上"刚才在想什么"的思路尾巴（对齐句首）+ 真实工具名，随后清空思路缓冲。
            _emit(
                "activity_progress",
                session_id=session_id,
                turn_id=turn_id,
                user_goal=user_goal,
                activity={"kind": "tool", "name": ev.tool_name},
                reasoning_hint=_clean_hint("".join(buf)),
            )
            buf.clear()
            pending[0] = ""
            rsince[0] = 0

    return cb


# ─── 高层工具：research(task) —— 背后是一整个子 Agent ───────────────────────


@mcp.tool()
async def research(task: str) -> str:
    """把一个研究任务外包给独立子 Agent：自己搜网页、读正文、归纳，返回综合答案。

    nano 只见这一个高层工具，拿回一段综合 summary（不直接持有底层 search/open_page）。
    子 Agent 的真实推理进展作为 subordinate 观测流播给语音编排器（需 STREAM_ENABLED=1
    才有逐工具叙事，否则只有一句通用播报）。

    ``async`` + ``anyio.to_thread`` offload：``run_child_loop`` 是同步阻塞循环，且内部
    工具调用也阻塞——必须挪进工作线程，否则卡死 FastMCP event loop（无法并发派发）。
    """
    return await anyio.to_thread.run_sync(_research_blocking, task)


def _research_blocking(task: str) -> str:
    """``research`` 的同步实现，在工作线程里跑。子失败也回 summary，不抛异常。"""
    # N3：per-call session_id（hash 跨进程不稳无妨，只需进程内 per-call 唯一，同 fake_web）。
    qid = abs(hash(task)) % 100000
    sid = f"{SESSION_PREFIX}-{qid}"
    tid = f"researcher-{qid}"
    # N7：user_goal 携带语义，措辞器据此说「在研究 X」而非凭空编。
    goal = f"研究：{task}"
    _record("research_call", {"session_id": sid, "turn_id": tid, "task": task})

    _emit("turn_started", session_id=sid, turn_id=tid, user_goal=goal)
    _emit(
        "activity_started",
        session_id=sid,
        turn_id=tid,
        user_goal=goal,
        activity={"kind": "thinking", "name": ""},
    )

    stream_enabled = _env_bool("STREAM_ENABLED", True)
    # 退化路径：不开流式则无逐工具事件，补一句通用中间播报，免得整轮只有开场+收尾。
    if not stream_enabled:
        _emit(
            "activity_progress",
            session_id=sid,
            turn_id=tid,
            user_goal=goal,
            activity={"kind": "tool", "name": "research", "completed_count": 0},
        )

    try:
        chain, model = _ensure_chain()
        result = run_child_loop(
            goal=task,
            context="",
            chain=chain,
            model=model,
            registry=_REGISTRY,
            allowed_tool_names=_ALLOWED_TOOLS,
            stream_enabled=stream_enabled,
            progress_callback=_make_voice_cb(sid, tid, goal),
        )
        summary = result.get("summary") or "（子 Agent 未产出摘要）"
    except Exception as exc:  # noqa: BLE001 - 子失败绝不掀翻工具调用
        result = None
        summary = f"研究失败：{type(exc).__name__}: {exc}"

    # 审计：记下这次 research 最终回给 nano 的 summary + 子 agent 内层轨迹（工具调用/轮数/
    # 退出原因），与发出去的 envelope 串成一条完整可审核链路。
    _record("research_result", {
        "session_id": sid,
        "turn_id": tid,
        "summary": summary,
        "exit_reason": (result or {}).get("exit_reason", "error"),
        "iterations": (result or {}).get("iterations", 0),
        "tool_trace": (result or {}).get("tool_trace", []),
    })

    _emit(
        "activity_started",
        session_id=sid,
        turn_id=tid,
        user_goal=goal,
        activity={"kind": "generating_text", "name": ""},
    )
    _emit("turn_finished", session_id=sid, turn_id=tid, user_goal=goal)
    return tool_result(output=summary)


if __name__ == "__main__":
    mcp.run(transport="stdio")  # /mcp connect stdio python search_agent_service.py
