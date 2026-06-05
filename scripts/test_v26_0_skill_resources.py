"""V26.0 Skill tier 3 资源不变量验证脚本。

不调真实 API；造一个 tmpdir 的 ``skills/<name>/`` 目录包（含 SKILL.md +
references/templates/assets/scripts 子目录）验证 SkillLoader 的 tier 3
发现/沙箱读取 + skill_view 工具双模式。

覆盖（10 项）：
list_resources（3）：
 1. 无任何资源子目录 → 空 dict
 2. 三类资源 → 三个 key 排序、路径相对 skill_dir
 3. 扩展名不在白名单（references/note.log）→ 不收
read_resource 沙箱（4）：
 4. ``../../etc/passwd`` 字面 .. → ValueError
 5. ``references/x.md`` 正常读 → (content, False)
 6. 沙箱内不存在文件 → FileNotFoundError
 7. symlink 指向 skill_dir 外 → resolve 后越界 → ValueError
skill_view 双模式（3）：
 8. 无 file_path → output + linked_files + usage_hint
 9. 有 file_path → output=该文件内容 + file 字段，无 linked_files
10. binary 文件 → is_binary=True 且 output 不含原始字节（只尺寸标记）
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


def _make_skill(root: Path, name: str, *, with_resources: bool) -> SkillLoader:
    """造 skills/<name>/SKILL.md（可选带 tier 3 资源），返回 scan 过的 loader。"""
    skill_dir = root / name
    _write(
        skill_dir / "SKILL.md",
        f"---\nname: {name}\ndescription: demo skill for v26.0\n---\n\n# {name}\n正文。\n",
    )
    if with_resources:
        _write(skill_dir / "references" / "checklist.md", "# Checklist\n- 项目一\n")
        _write(skill_dir / "templates" / "report.md", "# Report ${TITLE}\n")
        _write(skill_dir / "scripts" / "validate.py", "print('ok')\n")
        # 白名单之外的扩展名：不应被 list_resources 收录
        _write(skill_dir / "references" / "note.log", "noise\n")
        # binary 资产：非 utf-8 字节
        (skill_dir / "assets").mkdir(parents=True, exist_ok=True)
        (skill_dir / "assets" / "logo.bin").write_bytes(b"\x89PNG\x00\xff\xfe")
    loader = SkillLoader(root)
    loader.scan()
    return loader


# ─── list_resources ────────────────────────────────────────────────────


def test_1_no_resource_dirs_empty_dict():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "bare", with_resources=False)
        assert loader.list_resources("bare") == {}, "无资源子目录应返回空 dict"


def test_2_three_categories_sorted_relative():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        res = loader.list_resources("plan")
        # references / templates / scripts / assets 四类都有内容
        assert set(res.keys()) == {"references", "templates", "scripts", "assets"}, res
        assert res["references"] == ["references/checklist.md"], res["references"]
        assert res["templates"] == ["templates/report.md"], res["templates"]
        assert res["scripts"] == ["scripts/validate.py"], res["scripts"]
        # 路径相对 skill_dir，不含绝对前缀
        for files in res.values():
            for rel in files:
                assert not rel.startswith("/"), f"路径应相对 skill_dir: {rel}"


def test_3_extension_not_in_whitelist_excluded():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        res = loader.list_resources("plan")
        # note.log 不在 references 白名单（*.md/*.txt）→ 不收
        assert "references/note.log" not in res.get("references", []), res


# ─── read_resource 沙箱 ─────────────────────────────────────────────────


def test_4_literal_dotdot_raises_valueerror():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        try:
            loader.read_resource("plan", "../../etc/passwd")
        except ValueError as exc:
            assert "traversal" in str(exc).lower() or ".." in str(exc), exc
        else:
            raise AssertionError("字面 .. 应抛 ValueError")


def test_5_normal_text_read():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        content, is_binary = loader.read_resource("plan", "references/checklist.md")
        assert is_binary is False, "文本文件 is_binary 应为 False"
        assert "Checklist" in content, content


def test_6_missing_file_raises_filenotfound():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        try:
            loader.read_resource("plan", "references/nope.md")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("沙箱内不存在文件应抛 FileNotFoundError")


def test_7_symlink_escape_raises_valueerror():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        loader = _make_skill(root, "plan", with_resources=True)
        # 在 skill_dir 外造一个秘密文件，再用 symlink 从 skill 内指过去
        secret = root / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        link = root / "plan" / "assets" / "leak"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            print("  (skip symlink test: platform unsupported)")
            return
        try:
            loader.read_resource("plan", "assets/leak")
        except ValueError as exc:
            assert "escape" in str(exc).lower(), exc
        else:
            raise AssertionError("symlink 逃逸应在 resolve 后被 ValueError 拦")


# ─── skill_view 双模式 ──────────────────────────────────────────────────


def test_8_view_no_filepath_lists_linked_files():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        set_skill_loader(loader)
        out = json.loads(skill_view_handler({"name": "plan"}))
        assert "output" in out and "# plan" in out["output"], out
        assert "linked_files" in out, "tier 2 结果应含 linked_files"
        assert "references" in out["linked_files"], out["linked_files"]
        assert "usage_hint" in out, out


def test_9_view_with_filepath_returns_only_that_file():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        set_skill_loader(loader)
        out = json.loads(
            skill_view_handler({"name": "plan", "file_path": "references/checklist.md"})
        )
        assert "Checklist" in out["output"], out
        assert out.get("file") == "references/checklist.md", out
        assert out.get("is_binary") is False, out
        assert "linked_files" not in out, "tier 3 单文件结果不应再附 linked_files"


def test_10_binary_file_marked_no_raw_bytes():
    with tempfile.TemporaryDirectory() as td:
        loader = _make_skill(Path(td), "plan", with_resources=True)
        set_skill_loader(loader)
        out = json.loads(
            skill_view_handler({"name": "plan", "file_path": "assets/logo.bin"})
        )
        assert out.get("is_binary") is True, out
        assert "Binary file" in out["output"], out
        # 原始字节（PNG 魔数）不应出现在 output 里
        assert "PNG" not in out["output"] or "bytes" in out["output"], out
        assert "\x89" not in out["output"], "binary 原始字节不得进 output"


def main() -> None:
    tests = [
        test_1_no_resource_dirs_empty_dict,
        test_2_three_categories_sorted_relative,
        test_3_extension_not_in_whitelist_excluded,
        test_4_literal_dotdot_raises_valueerror,
        test_5_normal_text_read,
        test_6_missing_file_raises_filenotfound,
        test_7_symlink_escape_raises_valueerror,
        test_8_view_no_filepath_lists_linked_files,
        test_9_view_with_filepath_returns_only_that_file,
        test_10_binary_file_marked_no_raw_bytes,
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
