"""fake_web_service — 模拟网页搜索 / 浏览的 MCP 服务不变量测试。

两条链路各验一遍：
- **进程内**：直接 import ``fake_web_service`` 调 ``search`` / ``open_page``，
  验召回打分、正文确实来自映射文件、未收录与越界各自报错。
- **MCP dispatch**：经 ``mcp_manager`` connect 子进程，验两个工具被注册、
  经 registry 调用（与主 agent 同一路径）返回结构化 JSON。

Run::

    .venv/bin/python scripts/test_fake_web_service.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.bootstrap import connect_mcp_servers  # noqa: F401 (env-spec 解析校验)
from tools.mcp_client import mcp_manager
from tools.registry import registry

_REPO = Path(__file__).resolve().parent.parent
_FAKE_WEB = _REPO / "fake_web_service.py"

fws = importlib.import_module("fake_web_service")

# search/open_page 现为 async（叙事弧线 offload 进工作线程，见 fake_web_service 文档）。
# 进程内单测里把节奏 sleep 清零（否则每次调用 ~6.5s），再用 asyncio.run 同步取回 JSON。
fws._BEAT_PAUSE = 0.0
fws._TAIL_PAUSE = 0.0


def _call(coro) -> str:
    """同步跑一条 async 工具调用并返回其 JSON 串（弧线 sleep 已清零，emit 失败自吞）。"""
    return asyncio.run(coro)


# 任意一个已收录的 URL，供多用例复用。
_KNOWN_URL = "https://example.com/python"


# ---------------------------------------------------------------- 进程内单测

def test_1_search_recalls_by_keyword():
    """search 命中关键词的网页，返回 {url,title,snippet} 列表。"""
    out = json.loads(_call(fws.search("python")))
    assert out["query"] == "python"
    urls = [r["url"] for r in out["results"]]
    assert _KNOWN_URL in urls, f"python 应召回 {_KNOWN_URL}，实得 {urls!r}"
    top = out["results"][0]
    assert set(top) == {"url", "title", "snippet"}, f"结果字段不符：{top.keys()}"
    assert top["snippet"], "snippet 不应为空"


def test_2_search_empty_on_no_match():
    """无任何命中时返回空列表，而非报错。"""
    out = json.loads(_call(fws.search("绝不可能命中的关键词zzz")))
    assert out["results"] == [], f"无命中应空列表，实得 {out['results']!r}"


def test_3_search_title_outranks_body():
    """标题命中权重高于仅正文命中：'天气' 应让 weather-faq 排第一。"""
    out = json.loads(_call(fws.search("天气")))
    assert out["results"], "‘天气’应至少召回一条"
    assert out["results"][0]["url"] == "https://example.com/weather-faq", (
        f"标题含‘天气’的页应排首位，实得 {out['results'][0]['url']!r}"
    )


def test_3b_search_multiword_query_recalls():
    """按词打分：多词 query（空白分隔）逐词命中，不再整串子串比对而全空。

    回归 N10 审计暴露的 fake_web 控制面 bug——'MCP Python' 这类多词检索此前把整条 query
    当一个子串去配单词关键词 'mcp'，永远 0 命中，agent 据此误报"没找到"。分词后应召回。"""
    for q in ("MCP Python", "Model Context Protocol Python SDK", "Anthropic MCP 模型上下文协议"):
        out = json.loads(_call(fws.search(q)))
        assert out["results"], f"多词 query {q!r} 应至少召回一条，实得空"
    # 仍不过度匹配：纯噪声多词串保持空
    noise = json.loads(_call(fws.search("绝不可能 命中的 随机词zzz")))
    assert noise["results"] == [], f"纯噪声多词串应仍空，实得 {noise['results']!r}"


def test_4_open_page_returns_file_content():
    """open_page 返回的 content 与映射文件内容逐字一致（正文确实来自文件）。"""
    out = json.loads(_call(fws.open_page(_KNOWN_URL)))
    assert "error" not in out, f"已收录 URL 不应报错：{out}"
    assert out["url"] == _KNOWN_URL
    assert out["title"] == fws._PAGES[_KNOWN_URL]["title"]
    on_disk = (fws._FIXTURES / fws._PAGES[_KNOWN_URL]["file"]).read_text("utf-8")
    assert out["content"] == on_disk, "content 应与 fixture 文件逐字一致"


def test_5_open_page_unknown_url_errors():
    """未收录 URL 返回 {url,error}，不抛异常。"""
    out = json.loads(_call(fws.open_page("https://nope.invalid/x")))
    assert "error" in out and out["url"] == "https://nope.invalid/x"
    assert "未收录" in out["error"]


def test_6_open_page_sandbox_blocks_escape():
    """坏映射条目（../ 越界）被沙箱拦下，不读出 fixtures 目录之外。"""
    bad_url = "https://example.com/__evil__"
    fws._PAGES[bad_url] = {"title": "x", "file": "../../etc/passwd", "keywords": []}
    try:
        out = json.loads(_call(fws.open_page(bad_url)))
    finally:
        fws._PAGES.pop(bad_url, None)
    assert "error" in out and "沙箱" in out["error"], f"越界应被拦，实得 {out}"


def test_7_every_mapped_file_exists():
    """映射中每个 file 都真实存在且非空（fixtures 与 _PAGES 不脱节）。"""
    for url, meta in fws._PAGES.items():
        body = json.loads(_call(fws.open_page(url)))
        assert "error" not in body, f"{url} 正文缺失：{body}"
        assert body["content"].strip(), f"{url} 正文为空"


# ---------------------------------------------------------------- MCP dispatch

def test_8_connect_registers_both_tools():
    mcp_manager.connect("web", sys.executable, [str(_FAKE_WEB)])
    assert "web" in mcp_manager.connected_servers
    assert "mcp_web_search" in registry.tool_names
    assert "mcp_web_open_page" in registry.tool_names


def test_9_dispatch_search_then_open_page():
    """经 registry 调用两个工具：先 search 拿候选，再 open_page 取正文。"""
    raw = mcp_manager.call_tool("mcp_web_search", {"query": "mcp"})
    hits = json.loads(json.loads(raw)["output"])["results"]  # V21.4: tool_result(output=...)
    assert hits, "‘mcp’应召回 MCP 概览页"
    url = hits[0]["url"]
    raw2 = mcp_manager.call_tool("mcp_web_open_page", {"url": url})
    page = json.loads(json.loads(raw2)["output"])
    assert page["url"] == url and page["content"], f"open_page 应返回正文：{page}"


def _teardown():
    if "web" in mcp_manager.connected_servers:
        mcp_manager.disconnect("web")


def main() -> None:
    tests = [
        test_1_search_recalls_by_keyword,
        test_2_search_empty_on_no_match,
        test_3_search_title_outranks_body,
        test_3b_search_multiword_query_recalls,
        test_4_open_page_returns_file_content,
        test_5_open_page_unknown_url_errors,
        test_6_open_page_sandbox_blocks_escape,
        test_7_every_mapped_file_exists,
        test_8_connect_registers_both_tools,
        test_9_dispatch_search_then_open_page,
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
