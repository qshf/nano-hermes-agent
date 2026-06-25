"""v27.5 MCP-as-Agent（search_agent_service）— 不变量验证脚本。

不调真实 API：用 fake chain（提前编排好的 NormalizedResponse 序列）驱动子 Agent，
+ 真本地 registry，验证以下不变量。

覆盖：
1.  本地工具复用 — search/open_page 经本进程 registry 调通，与 fake_web 同形 JSON
2.  本地工具错误路径 — open_page 未收录 url → {url,error}，不抛
3.  research 子 Agent 综合 — fake chain：搜一次 → 出文本 → research 回该 summary（V21.4 协议）
4.  research tool_trace — 子内确实 dispatch 了 search（经 fake chain 第二轮 messages 验证）
5.  语音 envelope — turn_started + 中间 + turn_finished，全 subordinate、同一 per-call sid
6.  语音桥 — _make_voice_cb 收到 EVENT_TOOL_CALL_STARTED(search) → emit activity_progress(web_search)
7.  下游静音 — ORCH="" 时 _emit 不发 POST（guard 生效）
8.  MCP dispatch（端到端，可选）— 无 key 则 skip

覆盖外（前档已验，不重复）：
- run_child_loop 隔离/黑名单/max_iter：test_v23_0_delegate.py
- V21.4 dispatch 兜底：test_v21_4_tool_result_protocol.py
- fake_web 打分/沙箱：test_fake_web_service.py
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

import search_agent_service as svc
from transports.streaming import (
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL_STARTED,
    StreamEvent,
)
from transports.types import NormalizedResponse, ToolCall, Usage


# ─── Fake Chain（同 test_v23_0_delegate 的形状）──────────────────────────────


@dataclass
class _FakeChain:
    """按顺序吐预定义 NormalizedResponse；记录每次 call 的 messages 供断言。

    无 ``stream_call`` 属性 → run_child_loop 自动走同步退化路径（适合无 key 单测）。
    """

    responses: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def call(self, *, model: str, messages: list, tools: list) -> NormalizedResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": list(tools)})
        if not self.responses:
            raise RuntimeError("FakeChain ran out of responses")
        return self.responses.pop(0)


def _stop(text: str) -> NormalizedResponse:
    return NormalizedResponse(
        content=text,
        tool_calls=None,
        finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _tool(name: str, args: dict, call_id: str = "tc-1") -> NormalizedResponse:
    return NormalizedResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(args))],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
    )


# ─── 测试驱动 ───────────────────────────────────────────────────────────────


_passed = 0
_failed = 0


def _run(name: str, fn):
    global _passed, _failed
    try:
        fn()
    except AssertionError as e:
        _failed += 1
        print(f"  [FAIL] {name}")
        print(f"         {e}")
        traceback.print_exc(limit=2)
    except Exception as e:  # noqa: BLE001
        _failed += 1
        print(f"  [ERR ] {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
    else:
        _passed += 1
        print(f"  [PASS] {name}")


# ─── 1/2: 本地工具复用 + 错误路径 ───────────────────────────────────────────


def test_01_local_search_reuse():
    out = json.loads(svc._tool_search({"query": "python"}))
    assert out["query"] == "python", out
    assert isinstance(out["results"], list) and len(out["results"]) >= 1, out
    first = out["results"][0]
    assert {"url", "title", "snippet"} <= set(first.keys()), first


def test_02_local_open_page_and_error():
    ok = json.loads(svc._tool_open_page({"url": "https://example.com/python"}))
    assert "content" in ok and ok["content"], ok
    assert "error" not in ok, ok
    bad = json.loads(svc._tool_open_page({"url": "https://nope.invalid/x"}))
    assert "error" in bad and "content" not in bad, bad


# ─── 3/4: research 子 Agent 综合 + tool_trace ───────────────────────────────


def _install_fake_chain(responses) -> _FakeChain:
    fake = _FakeChain(responses=list(responses))
    svc._CHAIN = fake
    svc._MODEL = "fake-model"
    return fake


def test_03_research_synthesizes_summary():
    _install_fake_chain([_tool("search", {"query": "python"}), _stop("综合答案：Python 是解释型语言。")])
    out = json.loads(svc._research_blocking("python 是什么"))
    assert out == {"output": "综合答案：Python 是解释型语言。"}, out


def test_04_research_dispatched_search():
    fake = _install_fake_chain([_tool("search", {"query": "mcp"}), _stop("done")])
    svc._research_blocking("查 mcp")
    # 第 2 轮 call 的 messages 里应已含 search 的 tool 结果（证明子内 dispatch 真跑了）
    assert len(fake.calls) == 2, fake.calls
    round2 = fake.calls[1]["messages"]
    tool_msgs = [m for m in round2 if m.get("role") == "tool"]
    assert tool_msgs, round2
    payload = json.loads(tool_msgs[0]["content"])
    # dispatch 把 _tool_search 的 {"query","results"} 包进 V21.4，可能透传或裹 output
    blob = json.dumps(payload, ensure_ascii=False)
    assert "results" in blob, payload


# ─── 5: 语音 envelope 形状 + 同一 session_id ────────────────────────────────


def _collect_emits(fn):
    captured = []
    orig = svc._emit

    def fake_emit(event_type, *, session_id, turn_id, user_goal="", activity=None):
        captured.append(
            {"event_type": event_type, "session_id": session_id, "activity": activity}
        )

    svc._emit = fake_emit
    try:
        fn()
    finally:
        svc._emit = orig
    return captured


def test_05_voice_envelope_shape():
    _install_fake_chain([_stop("直接答")])
    # 关流式 → 走退化路径，会补一条通用 activity_progress
    old = os.environ.get("STREAM_ENABLED")
    os.environ["STREAM_ENABLED"] = "0"
    try:
        ev = _collect_emits(lambda: svc._research_blocking("水的沸点"))
    finally:
        if old is None:
            os.environ.pop("STREAM_ENABLED", None)
        else:
            os.environ["STREAM_ENABLED"] = old
    types = [e["event_type"] for e in ev]
    assert types[0] == "turn_started", types
    assert types[-1] == "turn_finished", types
    assert any(t in ("activity_progress", "activity_started") for t in types[1:-1]), types
    sids = {e["session_id"] for e in ev}
    assert len(sids) == 1, sids  # 同一 per-call session_id


# ─── 6: 语音桥 —— 转发 agent 真实思路为 reasoning_hint（不写死话术）─────────────


def test_06_voice_bridge_forwards_reasoning():
    captured = []
    orig = svc._emit
    svc._emit = lambda et, **kw: captured.append(
        (et, kw.get("activity"), kw.get("reasoning_hint"))
    )
    try:
        cb = svc._make_voice_cb("sid-x", "tid-x", "研究：x")
        # agent 先吐思路，再决定调 search → 该拍应带"刚才在想什么" + 真实工具名
        cb(StreamEvent(type=EVENT_REASONING_DELTA, text="我先查一下 MCP 的官方定义"))
        cb(StreamEvent(type=EVENT_TOOL_CALL_STARTED, tool_name="search"))
    finally:
        svc._emit = orig
    assert len(captured) == 1, captured
    et, activity, hint = captured[0]
    assert et == "activity_progress", captured
    # 工具名按原样转发（不映射、不写死），怎么说交给措辞器
    assert activity["kind"] == "tool" and activity["name"] == "search", activity
    assert "completed_count" not in activity, activity  # 无意义的 0 已去掉
    assert "MCP" in (hint or ""), hint  # agent 思路原文被转发为 reasoning_hint


def test_06b_reasoning_flush_feeds_hint():
    """思路流（reasoning_delta）攒够阈值且成整句应补发一拍 → 解决长思考静默。"""
    captured = []
    orig = svc._emit
    svc._emit = lambda et, **kw: captured.append(kw.get("reasoning_hint"))
    try:
        cb = svc._make_voice_cb("s", "t", "研究：x")
        # reasoning 含句子边界、超阈值 → 应补发一拍，hint 非空、不超长
        cb(StreamEvent(type=EVENT_REASONING_DELTA, text="我在分析 MCP 架构细节" + "据" * svc._FLUSH_EVERY_CHARS + "。"))
    finally:
        svc._emit = orig
    assert len(captured) >= 1 and captured[0], captured
    assert len(captured[0]) <= svc._HINT_MAX_CHARS, len(captured[0])  # 截断生效


def test_06b2_reply_text_never_enters_hint():
    """回复正文（text_delta）只发节奏拍，reasoning_hint 必须为空 —— 不复读答案。"""
    captured = []
    orig = svc._emit
    svc._emit = lambda et, **kw: captured.append((kw.get("activity"), kw.get("reasoning_hint")))
    try:
        cb = svc._make_voice_cb("s", "t", "研究：x")
        # 大段回复正文（含标题/句子），超阈值 → 发节奏拍，但 hint 必须空
        cb(StreamEvent(type=EVENT_TEXT_DELTA, text="## 一、MCP 协议概述。" + "甲" * svc._FLUSH_EVERY_CHARS))
    finally:
        svc._emit = orig
    assert len(captured) == 1, captured
    activity, hint = captured[0]
    assert activity["kind"] == "generating_text", activity
    assert hint == "", repr(hint)  # 答案正文绝不进 reasoning_hint


def test_06c_clean_hint_aligns_to_sentence_boundary():
    """_clean_hint：截断后丢掉开头半句，对齐到句子边界（修"断头话"）。"""
    # 构造超长串，前缀是会被截掉的半句，句号后是完整句
    long = "前半句被切掉的残文部分" * 30 + "。这是一个完整的句子结尾。"
    out = svc._clean_hint(long)
    assert len(out) <= svc._HINT_MAX_CHARS, len(out)
    # 截断后开头不应是半句残文：清理后应从某个句子边界之后开始（不以残文起头的概率）
    assert "。" not in out[:1], out  # 不以句号开头
    # 短串（未超长）原样返回，不强切
    assert svc._clean_hint("短句无需处理") == "短句无需处理"
    assert svc._clean_hint("") == ""


def test_06d_carry_remainder_no_midsentence_start():
    """reasoning 补发只发完整句，半句进位到下一拍 → 每拍 hint 整句起、整句止。"""
    captured = []
    orig = svc._emit
    svc._emit = lambda et, **kw: captured.append(kw.get("reasoning_hint"))
    try:
        cb = svc._make_voice_cb("s", "t", "研究：x")
        pad = "啊" * svc._FLUSH_EVERY_CHARS
        # 第一拍：一个完整句 + 拖一个半句（半句应被留下，不发出）
        cb(StreamEvent(type=EVENT_REASONING_DELTA, text="第一句完整内容。" + pad + "这里是半句没说完"))
        # 第二拍：补上句号收口前面的半句 + 再来一句
        cb(StreamEvent(type=EVENT_REASONING_DELTA, text="的剩余部分。" + pad + "第三句也完整。"))
    finally:
        svc._emit = orig
    assert len(captured) == 2, captured
    h1, h2 = captured
    # 第一拍发的应以句子边界收口，且不含未完成的"这里是半句"
    assert "这里是半句" not in h1, h1
    # 第二拍应衔接进位的半句（含"半句没说完的剩余部分"语义），而非从"的剩余部分"句中起头
    assert "半句没说完" in h2, h2  # 进位生效：上拍留下的半句出现在这拍


def test_06e_split_complete_carries_half():
    """_split_complete：在最后边界切，半句进位；无边界则继续攒（除非超安全阀）。"""
    complete, rest = svc._split_complete("第一句。第二句。还没完的")
    assert complete == "第一句。第二句。", complete
    assert rest == "还没完的", rest
    # 无边界且未超阀 → 全部留作余下，complete 空
    c2, r2 = svc._split_complete("一直没有标点的长串")
    assert c2 == "" and r2 == "一直没有标点的长串", (c2, r2)


# ─── 7: ORCH="" 静音 ────────────────────────────────────────────────────────


def test_07_mute_when_orch_empty():
    posted = {"n": 0}
    import urllib.request as ureq

    orig_url, orig_open = svc.ORCH, ureq.urlopen
    ureq.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not POST"))
    svc.ORCH = ""
    try:
        svc._emit("turn_started", session_id="s", turn_id="t")  # guard 应直接 return
        posted["n"] = 0  # 没抛 = 没走到 urlopen
    finally:
        svc.ORCH = orig_url
        ureq.urlopen = orig_open
    assert posted["n"] == 0


# ─── 8: MCP dispatch 端到端（可选，无 key skip）─────────────────────────────


def test_08_mcp_dispatch_optional():
    if not (os.environ.get("OPENAI_API_KEY") and os.environ.get("RUN_E2E") == "1"):
        print("  [SKIP] e2e MCP dispatch — 设 RUN_E2E=1 且有 OPENAI_API_KEY 才跑")
        return
    from tools.mcp_client import mcp_manager

    here = str(Path(__file__).resolve().parent.parent / "search_agent_service.py")
    mcp_manager.connect("researcher", sys.executable, [here])
    try:
        tools = mcp_manager.get_tools("researcher")
        assert "mcp_researcher_research" in tools, tools
        res = mcp_manager.call_tool("mcp_researcher_research", {"task": "python 是什么"})
        assert json.loads(res).get("output"), res
    finally:
        mcp_manager.disconnect("researcher")


# ─── main ───────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  v27.5 search_agent_service — 不变量验证")
    print("=" * 60)
    _run("01 本地 search 复用", test_01_local_search_reuse)
    _run("02 本地 open_page + 错误路径", test_02_local_open_page_and_error)
    _run("03 research 综合 summary", test_03_research_synthesizes_summary)
    _run("04 research 子内 dispatch search", test_04_research_dispatched_search)
    _run("05 语音 envelope 形状 + 同 sid", test_05_voice_envelope_shape)
    _run("06 语音桥转发真实思路 reasoning_hint", test_06_voice_bridge_forwards_reasoning)
    _run("06b reasoning 补发喂 hint", test_06b_reasoning_flush_feeds_hint)
    _run("06b2 回复正文不进 hint", test_06b2_reply_text_never_enters_hint)
    _run("06c clean_hint 对齐句子边界", test_06c_clean_hint_aligns_to_sentence_boundary)
    _run("06d 半句进位不从句中起头", test_06d_carry_remainder_no_midsentence_start)
    _run("06e split_complete 切分语义", test_06e_split_complete_carries_half)
    _run("07 ORCH='' 静音", test_07_mute_when_orch_empty)
    _run("08 MCP dispatch 端到端（可选）", test_08_mcp_dispatch_optional)
    print("=" * 60)
    print(f"  {_passed} passed, {_failed} failed")
    print("=" * 60)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
