"""V26.2 安全 token 替换 行为验证脚本。

验证 8 个关键不变量（计划 §7.4）：

token 替换（5）：
1. ${SKILL_DIR} → 替换成 skill 的绝对目录
2. ${SESSION_ID} 有值 → 替换；无值（None）→ 原样保留
3. 未知 token ${FOO} → 原样保留（不在白名单）
4. 无 token 的纯文本 → 原样不变
5. ${OPENAI_API_KEY} → **不替换**（不在白名单，密钥不泄露进 context）

接入（3）：
6. view() 输出含替换后的绝对路径（${SKILL_DIR} 真被替）
7. read_resource 返回的 tier 3 资源 **不**做 token 替换（模板语法不该被动）
8. 连续两个 token（${SKILL_DIR} 和 ${SESSION_ID}）都各自替换

设计原则：零外部依赖、秒级完成、自包含可执行。token 替换是纯字符串操作，
无需 Fake HTTP/DB —— 直接构造 SkillLoader 指向临时 skills 目录即可。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.skill_loader import SkillLoader
from agent.skill_preprocessing import substitute_tokens


def _make_skills_dir() -> Path:
    """建一个临时 skills/<name>/ 结构，含 SKILL.md + 一个 tier 3 模板。

    SKILL.md 正文里塞满各类 token（白名单内/外、连续、未知），让接入测试
    一次性覆盖。templates/tpl.md 里也放 ${SKILL_DIR}，验证 tier 3 不替换。
    """
    root = Path(tempfile.mkdtemp(prefix="nano_v26_2_"))
    demo = root / "demo"
    (demo / "templates").mkdir(parents=True)

    skill_md = demo / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: demo\n"
        'description: "token subst demo"\n'
        "---\n\n"
        "dir=${SKILL_DIR} sid=${SESSION_ID}\n"
        "unknown=${FOO} secret=${OPENAI_API_KEY}\n",
        encoding="utf-8",
    )
    # tier 3 模板：${SKILL_DIR} 是模板自身语法，read_resource 不该替它
    (demo / "templates" / "tpl.md").write_text(
        "template uses ${SKILL_DIR} literally\n", encoding="utf-8"
    )
    return root


def _make_loader() -> SkillLoader:
    loader = SkillLoader(_make_skills_dir())
    loader.scan()
    return loader


# ── token 替换（纯函数，不经 loader）─────────────────────────────────

def test_skill_dir_replaced() -> None:
    """1. ${SKILL_DIR} → 替换成给定目录的字符串。"""
    out = substitute_tokens("at ${SKILL_DIR}/x", Path("/tmp/s"), None)
    assert out == "at /tmp/s/x", out
    print("  skill_dir_replaced OK")


def test_session_id_present_and_absent() -> None:
    """2. ${SESSION_ID} 有值→替换；无值（None）→原样保留。"""
    with_sid = substitute_tokens("sid=${SESSION_ID}", None, "sess-42")
    assert with_sid == "sid=sess-42", with_sid
    no_sid = substitute_tokens("sid=${SESSION_ID}", None, None)
    assert no_sid == "sid=${SESSION_ID}", no_sid  # 无值原样保留，让作者排错
    print("  session_id_present_and_absent OK")


def test_unknown_token_preserved() -> None:
    """3. 未知 token ${FOO} → 原样保留（不在白名单正则捕获范围）。"""
    out = substitute_tokens("x=${FOO}", Path("/tmp/s"), "sid")
    assert out == "x=${FOO}", out
    print("  unknown_token_preserved OK")


def test_no_token_unchanged() -> None:
    """4. 无 token 的纯文本 → 原样不变。空串也原样返回。"""
    text = "plain text, no tokens here.\nsecond line."
    assert substitute_tokens(text, Path("/tmp/s"), "sid") == text
    assert substitute_tokens("", Path("/tmp/s"), "sid") == ""
    print("  no_token_unchanged OK")


def test_secret_env_not_replaced() -> None:
    """5. ${OPENAI_API_KEY} → 不替换（密钥不进 context），即使该 env 真有值。"""
    os.environ["OPENAI_API_KEY"] = "sk-should-not-leak"
    try:
        out = substitute_tokens("key=${OPENAI_API_KEY}", Path("/tmp/s"), "sid")
        assert out == "key=${OPENAI_API_KEY}", out
        assert "sk-should-not-leak" not in out
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
    print("  secret_env_not_replaced OK")


# ── 接入（经 SkillLoader）────────────────────────────────────────────

def test_view_substitutes() -> None:
    """6. view() 输出里 ${SKILL_DIR} 被替成该 skill 的绝对目录。"""
    loader = _make_loader()
    skill_dir = str(loader.get("demo").skill_dir)
    out = loader.view("demo", session_id="sess-9")
    assert f"dir={skill_dir}" in out, out
    assert "${SKILL_DIR}" not in out  # 已全替
    print("  view_substitutes OK")


def test_read_resource_not_substituted() -> None:
    """7. read_resource 返回的 tier 3 资源不做 token 替换（保留模板语法）。"""
    loader = _make_loader()
    content, is_binary = loader.read_resource("demo", "templates/tpl.md")
    assert not is_binary
    assert "${SKILL_DIR}" in content, content  # 原样保留，未被替
    print("  read_resource_not_substituted OK")


def test_consecutive_tokens_both_replaced() -> None:
    """8. 连续两个白名单 token（${SKILL_DIR} 与 ${SESSION_ID}）都各自替换。"""
    loader = _make_loader()
    skill_dir = str(loader.get("demo").skill_dir)
    out = loader.view("demo", session_id="sess-7")
    assert f"dir={skill_dir} sid=sess-7" in out, out
    # 同时确认白名单外的两个 token 仍原样
    assert "${FOO}" in out and "${OPENAI_API_KEY}" in out
    print("  consecutive_tokens_both_replaced OK")


def main() -> int:
    tests = [
        test_skill_dir_replaced,
        test_session_id_present_and_absent,
        test_unknown_token_preserved,
        test_no_token_unchanged,
        test_secret_env_not_replaced,
        test_view_substitutes,
        test_read_resource_not_substituted,
        test_consecutive_tokens_both_replaced,
    ]
    print(f"Running {len(tests)} V26.2 token-subst tests...")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
