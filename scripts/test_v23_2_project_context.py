"""V23.2 项目上下文注入 + --cwd 启动 不变量验证脚本。

不调真实 API；用 tmpdir 造各种 cwd 形态验证 _render_project_context 行为，
以及 main.py 的 argparse + chdir 入口（subprocess 起子进程检查 banner）。

覆盖（11 项）：
1. cwd=None → 项目上下文段返回空（V21.x 兼容）
2. cwd 存在但无任何上下文文件 → 项目上下文段返回空
3. cwd 含 nano-hermes-agent.md → 段渲染含 "## nano-hermes-agent.md" + 文件内容
4. cwd 同时含两个文件 → 优先用 nano-hermes-agent.md（AGENTS.md 不出现）
5. cwd 仅含 AGENTS.md → 段渲染含 "## AGENTS.md" + 内容
6. NANO_IGNORE_RULES=1 → 即使有上下文文件也整段跳过
7. 文件 > 20000 字符 → 头 60% + 尾 30% + 截断标记
8. cwd 不存在 / 不是目录 → 静默返回空（不抛）
9. 段顺序：骨架 → 项目上下文 → skill → memory → 工具列表
10. 同 cwd 多次 build() 输出严格相等（cache prefix 稳定性保证）
11. main.py --cwd <bad> 退出码 2（fail-fast）
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import PromptBuilder, SKELETON_PROMPT
from agent.prompt_builder import (
    PROJECT_CONTEXT_FILE_NAMES,
    PROJECT_CONTEXT_MAX_CHARS,
)


# ─── Fake 依赖（与 test_v21_2_prompt_builder 同款）──────────────────────


class _FakeMemoryManager:
    def __init__(self, system_prompt: str = "", tool_names=None):
        self._sp = system_prompt
        self._tools = tool_names or []

    def build_system_prompt(self) -> str:
        return self._sp

    def get_all_tool_names(self):
        return list(self._tools)


class _FakeSkill:
    def __init__(self, name, description):
        self.name = name
        self.description = description


class _FakeSkillLoader:
    def __init__(self, skills):
        self._skills = skills

    def list_metadata(self):
        return list(self._skills)


def _make_builder(*, cwd=None, skill_loader=None, memory_prompt="", memory_tools=None,
                  toolsets_return=None):
    return PromptBuilder(
        get_toolset_tool_names=lambda toolsets: list(toolsets_return or []),
        enabled_toolsets=["core"],
        memory_manager=_FakeMemoryManager(memory_prompt, memory_tools or []),
        skill_loader=skill_loader,
        cwd=cwd,
    )


def _clear_ignore_env():
    os.environ.pop("NANO_IGNORE_RULES", None)


# ─── 测试 ─────────────────────────────────────────────────────────────────


def test_cwd_none_skips_project_context():
    """V21.x 兼容：cwd=None 时段不渲染。"""
    pb = _make_builder(cwd=None, toolsets_return=["t1"])
    out = pb.build()
    assert "## nano-hermes-agent.md" not in out
    assert "## AGENTS.md" not in out


def test_cwd_with_no_context_files_renders_empty():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        pb = _make_builder(cwd=Path(td), toolsets_return=["t1"])
        out = pb.build()
        assert "## nano-hermes-agent.md" not in out
        assert "## AGENTS.md" not in out


def test_nano_hermes_agent_md_renders():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "nano-hermes-agent.md").write_text(
            "PROJECT-CTX-NANO\n\n这是测试项目。", encoding="utf-8"
        )
        pb = _make_builder(cwd=cwd)
        out = pb.build()
        assert "## nano-hermes-agent.md" in out
        assert "PROJECT-CTX-NANO" in out
        assert "这是测试项目。" in out


def test_nano_md_takes_priority_over_agents_md():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "nano-hermes-agent.md").write_text("FROM-NANO-MD", encoding="utf-8")
        (cwd / "AGENTS.md").write_text("FROM-AGENTS-MD", encoding="utf-8")
        pb = _make_builder(cwd=cwd)
        out = pb.build()
        assert "FROM-NANO-MD" in out
        assert "FROM-AGENTS-MD" not in out
        assert "## AGENTS.md" not in out


def test_agents_md_fallback():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "AGENTS.md").write_text("FROM-AGENTS-MD", encoding="utf-8")
        pb = _make_builder(cwd=cwd)
        out = pb.build()
        assert "## AGENTS.md" in out
        assert "FROM-AGENTS-MD" in out


def test_nano_ignore_rules_env_skips_section():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "nano-hermes-agent.md").write_text("SHOULD-NOT-APPEAR", encoding="utf-8")
        os.environ["NANO_IGNORE_RULES"] = "1"
        try:
            pb = _make_builder(cwd=cwd)
            out = pb.build()
            assert "SHOULD-NOT-APPEAR" not in out
            assert "## nano-hermes-agent.md" not in out
        finally:
            _clear_ignore_env()


def test_oversized_content_truncated():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        # 30k 字符 → 必须触发截断
        big = "A" * 30_000
        (cwd / "nano-hermes-agent.md").write_text(big, encoding="utf-8")
        pb = _make_builder(cwd=cwd)
        out = pb.build()
        assert "[...truncated nano-hermes-agent.md:" in out
        # 截断后整体长度 < 原始大小
        assert "A" * 30_000 not in out
        # 头尾保留：开头 12000 个 A 应在；结尾也必须 A
        assert "A" * 12_000 in out


def test_nonexistent_cwd_silently_empty():
    _clear_ignore_env()
    pb = _make_builder(cwd=Path("/nonexistent/path/does/not/exist/v23_2"))
    # 不抛 — 项目上下文段返回空，骨架仍在
    out = pb.build()
    assert "helpful coding assistant" in out
    assert "## nano-hermes-agent.md" not in out
    assert "## AGENTS.md" not in out


def test_section_order_skeleton_project_skill_memory_tools():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "nano-hermes-agent.md").write_text("PROJECT-MARKER", encoding="utf-8")
        loader = _FakeSkillLoader([_FakeSkill("plan", "Plan it")])
        pb = _make_builder(
            cwd=cwd,
            skill_loader=loader,
            memory_prompt="MEMORY-MARKER",
            memory_tools=["mtool"],
            toolsets_return=["btool"],
        )
        out = pb.build()
        skel = out.index("helpful coding assistant")
        proj = out.index("PROJECT-MARKER")
        skill = out.index("## available skills")
        memory = out.index("MEMORY-MARKER")
        tools = out.index("## available tools")
        assert skel < proj < skill < memory < tools


def test_build_is_pure_with_cwd():
    _clear_ignore_env()
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "nano-hermes-agent.md").write_text("STABLE", encoding="utf-8")
        pb = _make_builder(cwd=cwd, toolsets_return=["t"])
        a = pb.build()
        b = pb.build()
        c = pb.build()
        assert a == b == c


def test_main_py_rejects_bad_cwd():
    """fail-fast：--cwd 指向不存在目录 → 退出码 2，不进 agent loop。"""
    nano_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(nano_root / "main.py"),
         "--cwd", "/nonexistent/v23_2/path"],
        cwd=str(nano_root),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2, (
        f"expected exit code 2 for bad --cwd, got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "不存在或不是目录" in proc.stdout or "不存在或不是目录" in proc.stderr


# ─── runner ───────────────────────────────────────────────────────────────


def _run_all() -> int:
    tests = [(name, obj) for name, obj in globals().items()
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed.append(name)
    print()
    print(f"  {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
