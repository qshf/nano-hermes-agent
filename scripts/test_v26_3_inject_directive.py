"""V26.3 inject_directive（行为指令注入 system prompt）不变量验证脚本。

不调真实 API；造 tmpdir 的 skills/<name>/ 目录包，验证：
- frontmatter ``inject_directive`` 解析
- ``SkillMetadata.is_fully_available`` 门控（env 全设 + requires_tools 满足）
- ``PromptBuilder._render_behavioral_directives`` 渲染 + 门控过滤
- 向后兼容：无 directive 字段的旧 skill 不受影响

覆盖（10 项）：
解析（2）：
 1. inject_directive 抽成 str（多行块）
 2. 缺该字段 → 空串（向后兼容）
is_fully_available 门控（4）：
 3. env 全设 + requires_tools 满足 → True
 4. 缺 env → False
 5. 缺 tool → False
 6. available_tools=None（没传工具信息）→ 不卡 tool，只看 env
渲染（4）：
 7. 完全可用且有 directive → 段含 directive 文本
 8. 缺 env → 段不含该 skill（门控过滤）
 9. 有 directive 但缺 tool → 段不含该 skill
10. 无任何 directive → 整段为空串（build() 自动跳过空段）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import SkillLoader
from agent.prompt_builder import PromptBuilder


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_loader(root: Path, name: str, frontmatter_body: str) -> SkillLoader:
    _write(
        root / name / "SKILL.md",
        f"---\nname: {name}\ndescription: demo for v26.3\n{frontmatter_body}---\n\n# {name}\n正文。\n",
    )
    loader = SkillLoader(root)
    loader.scan()
    return loader


def _unset(*names: str) -> None:
    for n in names:
        os.environ.pop(n, None)


class _FakeMemoryManager:
    """PromptBuilder 依赖的最小桩 —— 无 memory 工具、空 system prompt 段。"""

    def get_all_tool_names(self):
        return []

    def build_system_prompt(self):
        return ""


def _make_builder(loader: SkillLoader, available_tools: list[str]) -> PromptBuilder:
    """造 PromptBuilder，toolset 解析器固定返回 available_tools。"""
    return PromptBuilder(
        get_toolset_tool_names=lambda _ts: list(available_tools),
        enabled_toolsets=["core"],
        memory_manager=_FakeMemoryManager(),
        skill_loader=loader,
    )


_DIRECTIVE_BODY = (
    "inject_directive: |\n"
    "  语音播报已就绪。关键节点必须主动调 nano-voice-say 播报。\n"
    "  起手播 info，完成播 done。\n"
)


# ─── 解析 ────────────────────────────────────────────────────────────────


def test_1_inject_directive_parsed():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(Path(td), "voice", _DIRECTIVE_BODY)
        meta = loader.get("voice")
        assert "必须主动调 nano-voice-say" in meta.inject_directive, repr(meta.inject_directive)
        assert "起手播 info" in meta.inject_directive


def test_2_missing_directive_empty_string():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(Path(td), "bare", "")
        meta = loader.get("bare")
        assert meta.inject_directive == "", repr(meta.inject_directive)


# ─── is_fully_available 门控 ─────────────────────────────────────────────


def test_3_fully_available_when_env_and_tools_met():
    _unset("V26_3_KEY")
    os.environ["V26_3_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as td:
            loader = _make_loader(
                Path(td), "voice",
                "required_environment_variables: [V26_3_KEY]\n"
                "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
            )
            meta = loader.get("voice")
            assert meta.is_fully_available(["terminal", "read_file"]) is True
    finally:
        _unset("V26_3_KEY")


def test_4_not_available_when_env_missing():
    _unset("V26_3_ABSENT")
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice",
            "required_environment_variables: [V26_3_ABSENT]\n"
            "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
        )
        meta = loader.get("voice")
        assert meta.is_fully_available(["terminal"]) is False


def test_5_not_available_when_tool_missing():
    _unset("V26_3_KEY")
    os.environ["V26_3_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as td:
            loader = _make_loader(
                Path(td), "voice",
                "required_environment_variables: [V26_3_KEY]\n"
                "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
            )
            meta = loader.get("voice")
            assert meta.is_fully_available(["read_file"]) is False  # 无 terminal
    finally:
        _unset("V26_3_KEY")


def test_6_available_tools_none_only_checks_env():
    _unset("V26_3_KEY")
    os.environ["V26_3_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as td:
            loader = _make_loader(
                Path(td), "voice",
                "required_environment_variables: [V26_3_KEY]\n"
                "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
            )
            meta = loader.get("voice")
            # None = 没传工具信息 → 不卡 tool，env 满足即可
            assert meta.is_fully_available(None) is True
    finally:
        _unset("V26_3_KEY")


# ─── 渲染 ────────────────────────────────────────────────────────────────


def test_7_render_includes_directive_when_available():
    _unset("V26_3_KEY")
    os.environ["V26_3_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as td:
            loader = _make_loader(
                Path(td), "voice",
                "required_environment_variables: [V26_3_KEY]\n"
                "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
            )
            builder = _make_builder(loader, available_tools=["terminal", "read_file"])
            section = builder._render_behavioral_directives()
            assert "behavioral directives" in section, section
            assert "必须主动调 nano-voice-say" in section, section
            # 也应进最终 system prompt
            full = builder.build()
            assert "必须主动调 nano-voice-say" in full
    finally:
        _unset("V26_3_KEY")


def test_8_render_excludes_when_env_missing():
    _unset("V26_3_ABSENT")
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice",
            "required_environment_variables: [V26_3_ABSENT]\n"
            "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
        )
        builder = _make_builder(loader, available_tools=["terminal"])
        section = builder._render_behavioral_directives()
        assert section == "", repr(section)  # 缺 env → 不注入


def test_9_render_excludes_when_tool_missing():
    _unset("V26_3_KEY")
    os.environ["V26_3_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as td:
            loader = _make_loader(
                Path(td), "voice",
                "required_environment_variables: [V26_3_KEY]\n"
                "metadata:\n  requires_tools: [terminal]\n" + _DIRECTIVE_BODY,
            )
            # terminal 不在可用工具里 → list_metadata 硬隐藏 + 门控双重保险
            builder = _make_builder(loader, available_tools=["read_file"])
            section = builder._render_behavioral_directives()
            assert section == "", repr(section)
    finally:
        _unset("V26_3_KEY")


def test_10_no_directive_renders_empty():
    with tempfile.TemporaryDirectory() as td:
        # skill 无 inject_directive 字段
        loader = _make_loader(
            Path(td), "plain", "metadata:\n  requires_tools: [terminal]\n"
        )
        builder = _make_builder(loader, available_tools=["terminal"])
        section = builder._render_behavioral_directives()
        assert section == "", repr(section)


def main() -> None:
    tests = [
        test_1_inject_directive_parsed,
        test_2_missing_directive_empty_string,
        test_3_fully_available_when_env_and_tools_met,
        test_4_not_available_when_env_missing,
        test_5_not_available_when_tool_missing,
        test_6_available_tools_none_only_checks_env,
        test_7_render_includes_directive_when_available,
        test_8_render_excludes_when_env_missing,
        test_9_render_excludes_when_tool_missing,
        test_10_no_directive_renders_empty,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except AssertionError as exc:
            print(f"✗ {t.__name__} — {exc}")
            failed += 1
        except Exception as exc:
            import traceback
            print(f"✗ {t.__name__} — UNEXPECTED {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failed += 1

    print("=" * 60)
    if failed:
        print(f"  FAIL: {failed}/{len(tests)} 不变量未通过")
        sys.exit(1)
    else:
        print(f"  PASS: {len(tests)}/{len(tests)} 不变量")


if __name__ == "__main__":
    main()
