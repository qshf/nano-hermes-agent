"""V21.2 PromptBuilder 三段式不变量验证脚本。

不调真实 API；用 fake memory_manager / fake skill_loader 验证段拼装行为。

覆盖：
1. 关 skill（loader 为 None）→ 输出无 skill 段
2. 开 skill（3 条 metadata）→ 输出含 "## available skills" + 3 行
3. skill_loader.list_metadata 抛异常 → skill 段静默退化为空（不影响整体）
4. skill_loader 返回空列表 → 不渲染 "## available skills" 标题
5. 段顺序固定：骨架 → skill → memory → tools
6. 段间空行严格一个（"\\n\\n" join）
7. 空段自动跳过 — memory 块为空 + 工具列表为空时只剩骨架
8. 关 memory（空 build_system_prompt 返回）→ 不输出 memory 段
9. 关工具（空列表）→ 不输出 "## available tools" 标题
10. 自定义 skeleton 注入 → 替换默认骨架
11. PromptBuilder.build() 是纯函数（同输入产出同输出）
12. memory_manager 与 skill_loader 重复工具名按 set 去重 + 排序
13. SKELETON_PROMPT 常量可被 import
14. AgentCtx.prompt_builder 字段为 Optional[Any] 且默认 None
15. /new 通过 ctx.prompt_builder.build() 重建 system prompt（优先于 build_system_prompt）
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import PromptBuilder, SKELETON_PROMPT
from agent.prompt_builder import PromptBuilder as PromptBuilderClass
from agent.runtime import AgentRuntime
import cli  # noqa: F401  触发命令注册（test 15 需要 /new handler）
from cli.context import AgentCtx
from cli.registry import dispatch


# ─── Fake 依赖 ─────────────────────────────────────────────────────────────


class _FakeMemoryManager:
    """最小 memory_manager — 让 build_system_prompt / get_all_tool_names 可控。"""

    def __init__(self, system_prompt: str = "", tool_names: list[str] | None = None):
        self._system_prompt = system_prompt
        self._tool_names = tool_names or []

    def build_system_prompt(self) -> str:
        return self._system_prompt

    def get_all_tool_names(self):
        return list(self._tool_names)


class _FakeSkill:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    def missing_env_vars(self):  # V26.1: 可用性门控接口（此处恒为「无缺失」）
        return []


class _FakeSkillLoader:
    def __init__(self, skills: list[_FakeSkill]):
        self._skills = skills

    # V26.1: list_metadata 接受可用工具/toolset（门控用）；fake 忽略它们全返回
    def list_metadata(self, available_tools=None, available_toolsets=None):
        return list(self._skills)


def _make_builder(
    *,
    toolsets_return: list[str] | None = None,
    memory_prompt: str = "",
    memory_tools: list[str] | None = None,
    skill_loader=None,
    skeleton: str | None = None,
) -> PromptBuilder:
    mm = _FakeMemoryManager(memory_prompt, memory_tools or [])
    kwargs = dict(
        get_toolset_tool_names=lambda toolsets: list(toolsets_return or []),
        enabled_toolsets=["core"],
        memory_manager=mm,
        skill_loader=skill_loader,
    )
    if skeleton is not None:
        kwargs["skeleton"] = skeleton
    return PromptBuilder(**kwargs)


# ─── 测试 ─────────────────────────────────────────────────────────────────


def test_skill_section_skipped_when_loader_none() -> None:
    pb = _make_builder(toolsets_return=["t1"], skill_loader=None)
    out = pb.build()
    assert "## available skills" not in out
    assert "## available tools" in out  # tools 段仍在


def test_skill_section_renders_three_skills() -> None:
    loader = _FakeSkillLoader([
        _FakeSkill("plan", "Plan a refactor"),
        _FakeSkill("tdd", "Test-driven development"),
        _FakeSkill("debug", "Systematic debugging"),
    ])
    pb = _make_builder(skill_loader=loader)
    out = pb.build()
    assert "## available skills" in out
    assert "- plan: Plan a refactor" in out
    assert "- tdd: Test-driven development" in out
    assert "- debug: Systematic debugging" in out


def test_skill_loader_exception_silent() -> None:
    bad_loader = MagicMock()
    bad_loader.list_metadata.side_effect = RuntimeError("boom")
    pb = _make_builder(skill_loader=bad_loader)
    out = pb.build()
    assert "## available skills" not in out
    # 骨架仍在
    assert "helpful coding assistant" in out


def test_skill_loader_empty_list_no_header() -> None:
    pb = _make_builder(skill_loader=_FakeSkillLoader([]))
    out = pb.build()
    assert "## available skills" not in out


def test_section_order_fixed() -> None:
    loader = _FakeSkillLoader([_FakeSkill("plan", "Plan it")])
    pb = _make_builder(
        toolsets_return=["t1"],
        memory_prompt="MEMORY-BLOCK",
        memory_tools=["m1"],
        skill_loader=loader,
    )
    out = pb.build()
    # 顺序：骨架 → skill → memory → tools
    skel_idx = out.index("helpful coding assistant")
    skill_idx = out.index("## available skills")
    memory_idx = out.index("MEMORY-BLOCK")
    tools_idx = out.index("## available tools")
    assert skel_idx < skill_idx < memory_idx < tools_idx


def test_section_separator_is_single_blank_line() -> None:
    pb = _make_builder(
        toolsets_return=["t1"],
        memory_prompt="MEMORY",
        memory_tools=["m1"],
    )
    out = pb.build()
    # "\n\n\n" 表示出现了双空行 — 不应该
    assert "\n\n\n" not in out


def test_empty_sections_skipped() -> None:
    """memory 空 + 工具空 → 仅剩骨架，无段分隔。"""
    pb = _make_builder(
        toolsets_return=[],
        memory_prompt="",
        memory_tools=[],
        skill_loader=None,
    )
    out = pb.build()
    assert out == SKELETON_PROMPT.strip()


def test_memory_section_skipped_when_empty() -> None:
    pb = _make_builder(toolsets_return=["t1"], memory_prompt="")
    out = pb.build()
    # tools 段在，memory 段不带可识别标记 — 仅检查 "##" 出现次数为 1（only tools）
    assert out.count("##") == 1


def test_tool_section_skipped_when_no_tools() -> None:
    pb = _make_builder(
        toolsets_return=[],
        memory_prompt="MEM",
        memory_tools=[],
    )
    out = pb.build()
    assert "## available tools" not in out


def test_custom_skeleton_injected() -> None:
    pb = _make_builder(skeleton="MY-CUSTOM-IDENTITY")
    out = pb.build()
    assert out.startswith("MY-CUSTOM-IDENTITY")
    assert "helpful coding assistant" not in out


def test_build_is_pure() -> None:
    pb = _make_builder(toolsets_return=["a", "b"], memory_prompt="MEM", memory_tools=["c"])
    a = pb.build()
    b = pb.build()
    c = pb.build()
    assert a == b == c


def test_tool_names_dedup_and_sort() -> None:
    pb = _make_builder(
        toolsets_return=["zeta", "alpha", "memory"],
        memory_tools=["memory", "beta"],   # "memory" 与 toolset 重叠
    )
    out = pb.build()
    tools_section = out[out.index("## available tools"):]
    # 出现顺序应该是 alpha → beta → memory → zeta
    expected_order = ["alpha", "beta", "memory", "zeta"]
    indices = [tools_section.index(f"- `{t}`") for t in expected_order]
    assert indices == sorted(indices)
    # memory 只出现一次
    assert tools_section.count("- `memory`") == 1


def test_skeleton_constant_importable() -> None:
    assert isinstance(SKELETON_PROMPT, str)
    assert "helpful coding assistant" in SKELETON_PROMPT
    # 同时验证 class 也能直接 import
    assert PromptBuilder is PromptBuilderClass


def test_agent_ctx_prompt_builder_field_optional() -> None:
    """AgentCtx 不传 prompt_builder 时默认为 None（V21.1 兼容性）。"""
    ctx = AgentCtx(
        messages=[],
        current_session_id="x",
        turn_count=0,
        chain=MagicMock(),
        client=MagicMock(),
        model="m",
        memory_manager=MagicMock(),
        builtin_provider=None,
        compressor=MagicMock(),
        registry=MagicMock(),
        enabled_toolsets=[],
        build_system_prompt=lambda: "fallback-sys",
        runtime=AgentRuntime(stream_enabled=True, cancel_token=None),
    )
    assert ctx.prompt_builder is None


def test_new_command_uses_prompt_builder_when_set() -> None:
    """/new handler 应优先调 ctx.prompt_builder.build()，否则回落 build_system_prompt。"""
    pb_built = []

    class _StubBuilder:
        def build(self) -> str:
            pb_built.append(True)
            return "FROM-PROMPT-BUILDER"

    fallback_called = []
    def _fallback() -> str:
        fallback_called.append(True)
        return "FROM-FALLBACK"

    mm = MagicMock()
    messages = [{"role": "system", "content": "old"}]
    ctx = AgentCtx(
        messages=messages,
        current_session_id="old-session",
        turn_count=99,
        chain=MagicMock(),
        client=MagicMock(),
        model="m",
        memory_manager=mm,
        builtin_provider=None,
        compressor=MagicMock(),
        registry=MagicMock(),
        enabled_toolsets=[],
        build_system_prompt=_fallback,
        prompt_builder=_StubBuilder(),
        runtime=AgentRuntime(stream_enabled=True, cancel_token=None),
    )
    handled = dispatch("/new", ctx)
    assert handled is True
    assert pb_built == [True], "PromptBuilder.build 应被调"
    assert fallback_called == [], "ctx.prompt_builder 存在时不应回落"
    assert ctx.messages[0]["content"] == "FROM-PROMPT-BUILDER"
    assert ctx.turn_count == 0
    assert ctx.current_session_id != "old-session"
    # mutate 应保持原 list 引用
    assert ctx.messages is messages


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
