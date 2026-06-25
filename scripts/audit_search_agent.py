"""审计 search_agent_service 究竟发出了什么数据。

为什么需要它
============
``search_agent_service`` 是 MCP **stdio** server：stdout 被协议占用、不能 print；
它发的 voice envelope 又只是 best-effort POST 给编排器，编排器没起 / 没存就丢了，
没法独立审核服务到底发了什么。服务现在支持 ``SEARCH_AGENT_LOG`` 落盘 tap（与 POST
解耦），本脚本把这条链路跑通并可读化。

两种用法
========
1. **离线自演示（默认，无需 key / 编排器）**：用 fake 流式 chain 驱动一次完整
   ``research``（search → open_page → 终段 compose），把服务发的每条数据落到临时
   JSONL，再回读、分组打印。看「究竟发送了什么」最快的方式::

       .venv/bin/python scripts/audit_search_agent.py

2. **审核真实跑出来的日志**：先在 .env 设 ``SEARCH_AGENT_LOG=logs/search_agent.jsonl``
   跑真链路，再把该文件传进来只做可读化打印（不再跑 fake）::

       .venv/bin/python scripts/audit_search_agent.py logs/search_agent.jsonl
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── 可读化打印（两种用法共用）──────────────────────────────────────────────


def _print_log(path: str) -> None:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    print(f"\n===== 审计日志 {path}（{len(rows)} 条）=====\n")
    n_env = 0
    for r in rows:
        kind = r.get("kind")
        ts = r.get("recorded_at")
        if kind == "research_call":
            print(f"┌─ ▶ research 调用  task={r.get('task')!r}")
        elif kind == "envelope":
            n_env += 1
            act = r.get("activity") or {}
            hint = r.get("reasoning_hint") or ""
            et = r.get("event_type")
            line = (
                f"│  [{et:16s}] activity={{{act.get('kind') or '-'}/{act.get('name') or '-'}}}"
                f" role={r.get('producer_role')}"
            )
            print(line)
            if hint:
                print(f"│      └ reasoning_hint({len(hint)}字): {hint!r}")
        elif kind == "research_result":
            tt = r.get("tool_trace") or []
            tools = " → ".join(t.get("tool", "?") for t in tt) or "(无工具调用)"
            print(f"└─ ◀ research 结果  exit={r.get('exit_reason')} iters={r.get('iterations')}"
                  f" tools=[{tools}]")
            print(f"      summary: {(r.get('summary') or '')[:120]!r}")
        print()  # spacer
    print(f"===== 共 {n_env} 条 envelope 发往编排器（含被静音/离线时仍落盘的）=====")


# ─── fake 流式 chain（离线自演示用，无需真 key）─────────────────────────────


def _build_fake_chain():
    """造一个有 ``stream_call`` 的 fake chain：3 轮 = 搜 → 开页 → 写答案。

    每轮先 yield 若干 reasoning/text delta（喂给语音桥），再 yield 一个 DONE 帧
    （``response`` 是等价 NormalizedResponse，驱动子 loop 真正 dispatch 工具）。
    """
    from transports.streaming import (
        EVENT_DONE, EVENT_REASONING_DELTA, EVENT_TEXT_DELTA,
        EVENT_TOOL_CALL_STARTED, StreamEvent,
    )
    from transports.types import NormalizedResponse, ToolCall, Usage

    def _usage():
        return Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30)

    def _ev(t, **kw):
        return StreamEvent(type=t, **kw)

    answer = (
        "Python 是一种解释型、动态类型的高级编程语言，以可读性著称。"
        "它用缩进定义代码块，强制统一风格；标准库丰富，生态有 numpy、pandas、fastapi 等；"
        "跨平台，广泛用于数据科学、Web 后端、自动化脚本与 AI 开发。"
    )

    scripts = [
        # 轮1：边想边决定调 search
        (
            [
                _ev(EVENT_REASONING_DELTA, text="The user asks what python is. "),
                _ev(EVENT_REASONING_DELTA, text="I'll search the web for an intro page."),
                _ev(EVENT_TOOL_CALL_STARTED, tool_name="search"),
            ],
            NormalizedResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="search", arguments=json.dumps({"query": "python"}))],
                finish_reason="tool_calls", usage=_usage(),
            ),
        ),
        # 轮2：看到结果，决定 open_page
        (
            [
                _ev(EVENT_REASONING_DELTA, text="Found the intro page. Let me open it to read details."),
                _ev(EVENT_TOOL_CALL_STARTED, tool_name="open_page"),
            ],
            NormalizedResponse(
                content=None,
                tool_calls=[ToolCall(id="c2", name="open_page",
                                     arguments=json.dumps({"url": "https://example.com/python"}))],
                finish_reason="tool_calls", usage=_usage(),
            ),
        ),
        # 轮3：终段 compose，逐字吐答案（无工具调用 → 靠周期 flush 发拍）
        (
            [_ev(EVENT_TEXT_DELTA, text=ch) for ch in answer],
            NormalizedResponse(content=answer, tool_calls=None, finish_reason="stop", usage=_usage()),
        ),
    ]

    @dataclass
    class _FakeStreamChain:
        i: int = 0

        def stream_call(self, *, cancel_token=None, model=None, messages=None, tools=None):
            events, resp = scripts[self.i]
            self.i += 1
            for ev in events:
                yield ev
            yield _ev(EVENT_DONE, response=resp)

    return _FakeStreamChain()


def _run_self_demo() -> None:
    log_path = os.path.join(tempfile.mkdtemp(prefix="search_agent_audit_"), "emits.jsonl")
    # 关键：在 import 服务**之前**设 env（模块级常量 _LOG_PATH / ORCH 在 import 时读取）。
    # ORCH="" → 不真 POST，但 _record 与 POST 解耦，照样落盘 → 证明"离线也能审核"。
    os.environ["SEARCH_AGENT_LOG"] = log_path
    os.environ["VOICE_ORCHESTRATOR_URL"] = ""
    os.environ["STREAM_ENABLED"] = "1"

    import search_agent_service as svc

    svc._CHAIN = _build_fake_chain()
    svc._MODEL = "fake-stream-model"

    print("跑一次 research（fake 流式 chain，无需 key / 编排器，ORCH 静音）…")
    out = svc._research_blocking("python 是什么")
    print(f"research 返回给 nano 的 tool_result：{out}")
    _print_log(log_path)
    print(f"\n原始日志文件：{log_path}")


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if not os.path.isfile(path):
            print(f"找不到日志文件：{path}")
            sys.exit(1)
        _print_log(path)
    else:
        _run_self_demo()


if __name__ == "__main__":
    main()
