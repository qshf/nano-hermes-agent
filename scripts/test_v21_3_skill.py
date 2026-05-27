"""V21.3 Skill 系统不变量验证脚本。

不调真实 API；造一个 tmpdir 的 ``skills/<name>/SKILL.md`` 树验证 SkillLoader
+ skill_view tool + /skill 命令 + PromptBuilder 集成。

覆盖（13 项）：
1. SkillLoader.scan 解析合规 frontmatter，list_metadata 按 name 排序
2. SkillLoader.view 返回完整 markdown（含 frontmatter）
3. SkillLoader.view 未知名 → KeyError
4. parse_frontmatter 无 frontmatter 时返回 ({}, original_text)
5. parse_frontmatter 带 frontmatter 时，body 不含分隔符
6. skill_matches_platform：当前平台匹配 → True；不匹配 → False；缺省 → True
7. scan 跳过单个 yaml 错误的 skill（不 take down 其它 skill）
8. scan 单个 skill 缺 ``name`` / ``description`` 字段 → KeyError
9. tools/skill_view_tool 在 loader 未注入时返回 error JSON 且 check_fn 为 False
10. tools/skill_view_tool 注入 loader 后，handler 命中 → 返回完整 markdown
11. tools/skill_view_tool 未知 skill → 返回 error JSON 列出可用 skill
12. /skill list / /skill view <name> / /skill reload 三个子命令在 dispatch 下都能跑
13. PromptBuilder 注入 SkillLoader 后 build() 包含 "## available skills" + 每条按 name 排序
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import (
    PromptBuilder,
    SkillLoader,
    SkillMetadata,
    parse_frontmatter,
    skill_matches_platform,
)
from tools.registry import registry
from tools.skill_view_tool import (
    SKILL_VIEW_SCHEMA,
    set_skill_loader,
    skill_view_handler,
    _check_skill_loader_ready,
)
import cli  # noqa: F401  — 触发命令注册（含 /skill）
from cli.context import AgentCtx
from cli.registry import dispatch


# ─── 测试夹具 ──────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_tmp_skills(tmp: Path) -> Path:
    """造 3 个合规 skill + 1 个 yaml 损坏 + 1 个 platform 不匹配。"""
    skills_dir = tmp / "skills"

    _write(
        skills_dir / "alpha" / "SKILL.md",
        "---\nname: alpha\ndescription: First skill alphabetically.\n---\n\n# Alpha\nbody-a\n",
    )
    _write(
        skills_dir / "beta" / "SKILL.md",
        "---\nname: beta\ndescription: Second skill.\nplatforms: [linux, macos, windows]\n---\n\nbody-b\n",
    )
    _write(
        skills_dir / "gamma" / "SKILL.md",
        "---\nname: gamma\ndescription: Third skill.\n---\n\nbody-g\n",
    )
    # yaml 损坏
    _write(
        skills_dir / "broken-yaml" / "SKILL.md",
        "---\n[unclosed: bracket\n---\nbody\n",
    )
    # 不匹配的平台（用一个 nano 永远不会跑的平台）
    _write(
        skills_dir / "irrelevant" / "SKILL.md",
        "---\nname: irrelevant\ndescription: Wrong platform.\nplatforms: [aix]\n---\n\nbody\n",
    )
    return skills_dir


def _fake_memory_manager() -> SimpleNamespace:
    return SimpleNamespace(
        build_system_prompt=lambda: "",
        get_all_tool_names=lambda: [],
    )


# ─── 测试 ──────────────────────────────────────────────────────────────────


def test_1_scan_and_list_metadata():
    with tempfile.TemporaryDirectory() as td:
        skills_dir = _build_tmp_skills(Path(td))
        loader = SkillLoader(skills_dir)

        buf = io.StringIO()
        with redirect_stdout(buf):
            loader.scan()
        # broken-yaml 应被打印 warning 跳过；不阻断
        assert "parse failed" in buf.getvalue(), buf.getvalue()

        metas = loader.list_metadata()
        names = [m.name for m in metas]
        # alpha/beta/gamma 三个；irrelevant 平台不匹配被过滤；broken-yaml yaml 错被跳
        assert names == ["alpha", "beta", "gamma"], names
        # 排序稳定 + frozen dataclass 字段对齐
        assert all(isinstance(m, SkillMetadata) for m in metas)
    print("✓ test 1 — scan + list_metadata 排序 + yaml 损坏跳过")


def test_2_view_returns_full_markdown():
    with tempfile.TemporaryDirectory() as td:
        skills_dir = _build_tmp_skills(Path(td))
        loader = SkillLoader(skills_dir)
        with redirect_stdout(io.StringIO()):
            loader.scan()
        text = loader.view("alpha")
        assert text.startswith("---"), text[:30]
        assert "name: alpha" in text
        assert "body-a" in text
    print("✓ test 2 — view 返回完整 markdown 含 frontmatter")


def test_3_view_unknown_raises_keyerror():
    with tempfile.TemporaryDirectory() as td:
        loader = SkillLoader(_build_tmp_skills(Path(td)))
        with redirect_stdout(io.StringIO()):
            loader.scan()
        try:
            loader.view("does-not-exist")
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError for unknown skill")
    print("✓ test 3 — view 未知名 → KeyError")


def test_4_parse_frontmatter_none():
    fm, body = parse_frontmatter("# just markdown\n\nno frontmatter")
    assert fm == {}, fm
    assert body == "# just markdown\n\nno frontmatter"
    print("✓ test 4 — 无 frontmatter 时返回 ({}, original)")


def test_5_parse_frontmatter_split():
    src = "---\nname: x\ndescription: y\n---\n\nthe body\n"
    fm, body = parse_frontmatter(src)
    assert fm == {"name": "x", "description": "y"}, fm
    assert body.strip() == "the body", repr(body)
    assert "---" not in body
    print("✓ test 5 — frontmatter 切片，body 不含分隔符")


def test_6_platform_matching():
    assert skill_matches_platform({}) is True
    assert skill_matches_platform({"platforms": []}) is True
    # 当前平台必匹配
    current_label = "macos" if sys.platform.startswith("darwin") else (
        "linux" if sys.platform.startswith("linux") else "windows"
    )
    assert skill_matches_platform({"platforms": [current_label]}) is True
    # 用永远不会匹配的标识
    assert skill_matches_platform({"platforms": ["aix"]}) is False
    print("✓ test 6 — platform 匹配（当前匹配 / 缺省匹配 / 不匹配 → False）")


def test_7_scan_skips_broken_yaml():
    """test_1 已隐式覆盖；这里显式再断言：broken-yaml 跳过后其它 3 个仍可加载。"""
    with tempfile.TemporaryDirectory() as td:
        skills_dir = _build_tmp_skills(Path(td))
        loader = SkillLoader(skills_dir)
        with redirect_stdout(io.StringIO()):
            loader.scan()
        assert len(loader) == 3
        assert loader.has("alpha") and loader.has("beta") and loader.has("gamma")
        assert not loader.has("broken-yaml")
    print("✓ test 7 — yaml 损坏单 skill 跳过，其它 skill 可用")


def test_8_missing_required_field_raises():
    with tempfile.TemporaryDirectory() as td:
        skills_dir = Path(td) / "skills"
        # 缺 description
        _write(
            skills_dir / "incomplete" / "SKILL.md",
            "---\nname: incomplete\n---\nbody\n",
        )
        loader = SkillLoader(skills_dir)
        try:
            loader.scan()
        except KeyError as exc:
            assert "description" in str(exc), str(exc)
        else:
            raise AssertionError("expected KeyError for missing description")
    print("✓ test 8 — frontmatter 缺必填字段 → KeyError")


def test_9_skill_view_tool_unset():
    set_skill_loader(None)
    assert _check_skill_loader_ready() is False
    raw = skill_view_handler({"name": "alpha"})
    parsed = json.loads(raw)
    assert "error" in parsed and "not initialized" in parsed["error"], parsed
    print("✓ test 9 — skill_view 未注入 loader 时返回 error JSON + check_fn=False")


def test_10_skill_view_tool_hit():
    with tempfile.TemporaryDirectory() as td:
        loader = SkillLoader(_build_tmp_skills(Path(td)))
        with redirect_stdout(io.StringIO()):
            loader.scan()
        set_skill_loader(loader)
        assert _check_skill_loader_ready() is True

        raw = skill_view_handler({"name": "beta"})
        parsed = json.loads(raw)
        assert "output" in parsed, parsed
        assert "name: beta" in parsed["output"]
        assert "body-b" in parsed["output"]

        # schema 自身合规
        assert SKILL_VIEW_SCHEMA["name"] == "skill_view"
        assert "name" in SKILL_VIEW_SCHEMA["parameters"]["required"]
    set_skill_loader(None)
    print("✓ test 10 — skill_view 注入 loader 后命中并返回完整 markdown")


def test_11_skill_view_tool_unknown():
    with tempfile.TemporaryDirectory() as td:
        loader = SkillLoader(_build_tmp_skills(Path(td)))
        with redirect_stdout(io.StringIO()):
            loader.scan()
        set_skill_loader(loader)
        raw = skill_view_handler({"name": "no-such-skill"})
        parsed = json.loads(raw)
        assert "error" in parsed
        # 列出 available
        assert "alpha" in parsed["error"]
    set_skill_loader(None)
    print("✓ test 11 — skill_view 未知名返回 error JSON 含可用列表")


def test_12_skill_slash_subcommands():
    with tempfile.TemporaryDirectory() as td:
        loader = SkillLoader(_build_tmp_skills(Path(td)))
        with redirect_stdout(io.StringIO()):
            loader.scan()

        ctx = AgentCtx(
            messages=[],
            current_session_id="x",
            turn_count=0,
            chain=None,
            client=None,
            model="m",
            memory_manager=_fake_memory_manager(),
            builtin_provider=None,
            compressor=None,
            registry=registry,
            enabled_toolsets=["core"],
            build_system_prompt=lambda: "",
            prompt_builder=None,
            skill_loader=loader,
        )

        # /skill list
        buf = io.StringIO()
        with redirect_stdout(buf):
            handled = dispatch("/skill list", ctx)
        assert handled is True
        out = buf.getvalue()
        assert "3 loaded" in out and "alpha" in out and "beta" in out and "gamma" in out, out

        # /skill view <name>
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch("/skill view alpha", ctx)
        out = buf.getvalue()
        assert "name: alpha" in out and "body-a" in out, out

        # /skill view <unknown>
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch("/skill view nope", ctx)
        assert "unknown" in buf.getvalue()

        # /skill reload — 数字应不变（同一目录）
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch("/skill reload", ctx)
        out = buf.getvalue()
        assert "3 → 3" in out, out

        # 默认子命令（无参 → list）
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch("/skill", ctx)
        assert "3 loaded" in buf.getvalue()

        # ctx 没挂 loader 时给统一文案
        ctx.skill_loader = None
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch("/skill list", ctx)
        assert "no skill loader attached" in buf.getvalue()
    print("✓ test 12 — /skill list / view / reload / 默认 + 未挂载兜底文案")


def test_13_prompt_builder_renders_index():
    with tempfile.TemporaryDirectory() as td:
        loader = SkillLoader(_build_tmp_skills(Path(td)))
        with redirect_stdout(io.StringIO()):
            loader.scan()

        pb = PromptBuilder(
            get_toolset_tool_names=lambda _toolsets: [],
            enabled_toolsets=["core"],
            memory_manager=_fake_memory_manager(),
            skill_loader=loader,
        )
        prompt = pb.build()
        assert "## available skills" in prompt
        # 段顺序：骨架 → skill 索引（memory/tools 都为空时不渲染）
        skel_idx = prompt.find("You are a helpful coding assistant.")
        skill_idx = prompt.find("## available skills")
        assert 0 <= skel_idx < skill_idx, (skel_idx, skill_idx)
        # 每条 skill 行按 name 排序
        for name in ("alpha", "beta", "gamma"):
            assert f"- {name}:" in prompt
        # description 落在行尾（不被截）
        assert "First skill alphabetically." in prompt
    print("✓ test 13 — PromptBuilder 注入 loader 后渲染按序的 skill 索引段")


# ─── 主函数 ────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  V21.3 Skill 系统 — 不变量验证")
    print("=" * 60)

    tests = [
        test_1_scan_and_list_metadata,
        test_2_view_returns_full_markdown,
        test_3_view_unknown_raises_keyerror,
        test_4_parse_frontmatter_none,
        test_5_parse_frontmatter_split,
        test_6_platform_matching,
        test_7_scan_skips_broken_yaml,
        test_8_missing_required_field_raises,
        test_9_skill_view_tool_unset,
        test_10_skill_view_tool_hit,
        test_11_skill_view_tool_unknown,
        test_12_skill_slash_subcommands,
        test_13_prompt_builder_renders_index,
    ]

    failed = 0
    for t in tests:
        try:
            t()
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
