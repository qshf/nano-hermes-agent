"""V26.1 Skill 可用性门控不变量验证脚本。

不调真实 API；造 tmpdir 的 ``skills/<name>/`` 目录包，验证 frontmatter 门控
字段解析 + env 软标记 + requires_tools/toolsets 硬隐藏 + skill_view 可用性回填。

覆盖（10 项）：
字段解析（3）：
 1. 顶层 ``required_environment_variables`` 抽成 tuple
 2. ``metadata.requires_tools`` / ``requires_toolsets`` 抽成 tuple
 3. 缺这些字段 → 空 tuple，不报错（向后兼容旧 SKILL.md）
env 软标记（3）：
 4. 声明的 env 全设置 → setup_needed=False、missing_env_vars()==[]
 5. 缺一个 env → missing_env_vars 含它、setup_needed=True
 6. env 缺失 **仍出现在 list_metadata**（软标记不过滤）
requires_tools 硬隐藏（4）:
 7. requires_tools 在 available → 出现
 8. requires_tools 不在 available → 不出现
 9. available_tools=None（无信息）→ 全显示（向后兼容）
10. requires_toolsets 不在 available → 不出现 + skill_view 回填 readiness_status
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import SkillLoader
from tools.skill_view_tool import set_skill_loader, skill_view_handler


# ─── 夹具 ──────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_loader(root: Path, name: str, frontmatter_body: str) -> SkillLoader:
    """造 skills/<name>/SKILL.md（frontmatter_body 是 --- 之间的内容），scan 返回 loader。"""
    _write(
        root / name / "SKILL.md",
        f"---\nname: {name}\ndescription: demo for v26.1\n{frontmatter_body}---\n\n# {name}\n正文。\n",
    )
    loader = SkillLoader(root)
    loader.scan()
    return loader


def _unset(*names: str) -> None:
    for n in names:
        os.environ.pop(n, None)


# ─── 字段解析 ────────────────────────────────────────────────────────────


def test_1_required_env_vars_parsed():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice",
            "required_environment_variables:\n  - DASHSCOPE_API_KEY\n  - OTHER_KEY\n",
        )
        meta = loader.get("voice")
        assert meta.required_env_vars == ("DASHSCOPE_API_KEY", "OTHER_KEY"), meta.required_env_vars


def test_2_requires_tools_and_toolsets_parsed():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice",
            "metadata:\n  requires_tools: [terminal]\n  requires_toolsets: [core]\n",
        )
        meta = loader.get("voice")
        assert meta.requires_tools == ("terminal",), meta.requires_tools
        assert meta.requires_toolsets == ("core",), meta.requires_toolsets


def test_3_missing_gating_fields_empty_tuples():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(Path(td), "bare", "")
        meta = loader.get("bare")
        assert meta.required_env_vars == (), meta.required_env_vars
        assert meta.requires_tools == (), meta.requires_tools
        assert meta.requires_toolsets == (), meta.requires_toolsets


# ─── env 软标记 ──────────────────────────────────────────────────────────


def test_4_all_env_set_not_setup_needed():
    _unset("V26_1_TEST_KEY")
    os.environ["V26_1_TEST_KEY"] = "x"
    try:
        with tempfile.TemporaryDirectory() as td:
            loader = _make_loader(
                Path(td), "voice", "required_environment_variables: [V26_1_TEST_KEY]\n"
            )
            meta = loader.get("voice")
            assert meta.missing_env_vars() == [], meta.missing_env_vars()
            assert meta.setup_needed is False
    finally:
        _unset("V26_1_TEST_KEY")


def test_5_missing_env_flagged():
    _unset("V26_1_ABSENT_KEY")
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice", "required_environment_variables: [V26_1_ABSENT_KEY]\n"
        )
        meta = loader.get("voice")
        assert meta.missing_env_vars() == ["V26_1_ABSENT_KEY"], meta.missing_env_vars()
        assert meta.setup_needed is True


def test_6_missing_env_still_listed():
    """软标记策略：env 缺失不从 list_metadata 过滤（agent 需要引导用户配置的机会）。"""
    _unset("V26_1_ABSENT_KEY")
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice", "required_environment_variables: [V26_1_ABSENT_KEY]\n"
        )
        names = [m.name for m in loader.list_metadata()]
        assert "voice" in names, names


# ─── requires_tools 硬隐藏 ───────────────────────────────────────────────


def test_7_requires_tool_present_shown():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(Path(td), "voice", "metadata:\n  requires_tools: [terminal]\n")
        names = [m.name for m in loader.list_metadata(available_tools=["terminal", "skill_view"])]
        assert "voice" in names, names


def test_8_requires_tool_absent_hidden():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(Path(td), "voice", "metadata:\n  requires_tools: [terminal]\n")
        names = [m.name for m in loader.list_metadata(available_tools=["skill_view"])]
        assert "voice" not in names, names


def test_9_available_none_shows_all():
    """向后兼容：调用方不传工具信息 → 不做硬隐藏（V21.x 行为不变）。"""
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(Path(td), "voice", "metadata:\n  requires_tools: [terminal]\n")
        names = [m.name for m in loader.list_metadata()]
        assert "voice" in names, names


def test_10_requires_toolset_absent_hidden_and_view_readiness():
    _unset("V26_1_ABSENT_KEY")
    with tempfile.TemporaryDirectory() as td:
        loader = _make_loader(
            Path(td), "voice",
            "required_environment_variables: [V26_1_ABSENT_KEY]\n"
            "metadata:\n  requires_toolsets: [core]\n",
        )
        # toolset 不在 available → 硬隐藏
        names = [
            m.name
            for m in loader.list_metadata(available_tools=[], available_toolsets=["other"])
        ]
        assert "voice" not in names, names
        # toolset 在 available → 出现
        names2 = [
            m.name
            for m in loader.list_metadata(available_tools=[], available_toolsets=["core"])
        ]
        assert "voice" in names2, names2
        # skill_view 回填 readiness（env 缺失 → setup_needed）
        set_skill_loader(loader)
        out = json.loads(skill_view_handler({"name": "voice"}))
        assert out.get("readiness_status") == "setup_needed", out
        assert out.get("missing_env_vars") == ["V26_1_ABSENT_KEY"], out


def main() -> None:
    tests = [
        test_1_required_env_vars_parsed,
        test_2_requires_tools_and_toolsets_parsed,
        test_3_missing_gating_fields_empty_tuples,
        test_4_all_env_set_not_setup_needed,
        test_5_missing_env_flagged,
        test_6_missing_env_still_listed,
        test_7_requires_tool_present_shown,
        test_8_requires_tool_absent_hidden,
        test_9_available_none_shows_all,
        test_10_requires_toolset_absent_hidden_and_view_readiness,
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
