"""V21.2: 三段式系统 prompt 拼装器。

为什么要做
==========
V4 引入的 ``build_system_prompt`` 是 10 行 ``str.format`` 替换 — 名义上叫
"系统 prompt 构建器"但**没有任何分段抽象**。要再加一段（例如 V21.3 的
skill 索引、未来的安全护栏、风格约束），唯一扩展点是改 ``SYSTEM_PROMPT``
模板字符串和加新参数，模板里看不出"这一段是干什么的"。

设计要点（仿源项目 ``agent/prompt_builder.py`` 1456 行的核心思路，去掉
上下文文件扫描 / 注入检测 / GitHub 同步 / 平台提示等几千行胶水）
=============================================================
- ``PromptBuilder.build()`` 返回 ``"\\n\\n".join(...)`` — 段间用空行分隔
- 每段都是独立方法 ``_render_<section>``，返回 str（空 str 表示该段跳过）
- 段顺序固定：骨架 → skill 索引 → memory 块 → 工具列表
- skill_loader 为 None（V21.2 阶段）时 skill 段返回 "" 自动跳过 →
  V21.3 把 ``SkillLoader`` 注入进来即可，V21.2 自身无需做 skill 渲染逻辑
- 段内自含标题（``## section title`` 或类似），骨架段不带标题（它就是身份段）
- 所有段渲染都是纯函数 — 同样的依赖输入产出同样的字符串，便于单测

为什么 V21.2 就把 skill 段位置占住而不等 V21.3
=============================================
段顺序固定是"上下文经济学"的关键：system prompt 必须在 V20 prompt cache
打 ephemeral 标记的位置稳定 — 每次重建都要保证 prefix 字节一致。如果
V21.3 才加 skill 段，会改动现有段顺序 → 旧 cache 全失配。V21.2 先占位
（``_render_skill_index`` 返回空），V21.3 只填充实现，cache 命中率不受影响。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional


SKELETON_PROMPT = """You are a helpful coding assistant.

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user."""


class PromptBuilder:
    """三段式系统 prompt 拼装器。

    构造期接收所有渲染依赖（toolset 解析器、memory_manager、skill_loader），
    每次 ``build()`` 调用时各段独立渲染再 join。空段自动跳过。

    Parameters
    ----------
    get_toolset_tool_names :
        回调，传入 ``enabled_toolsets`` 列表返回内置 toolset 工具名集合
        （V21.1 之前 main.py 直接调 ``get_available_tool_names``）
    enabled_toolsets :
        当前启用的 toolset 列表（如 ``["core"]``）
    memory_manager :
        ``MemoryManager`` 实例 — 用于提供 memory 段 + provider 工具名
    skill_loader :
        V21.3 才注入；V21.2 阶段始终为 None
    skeleton :
        骨架 prompt 文本，默认用模块常量 ``SKELETON_PROMPT``。注入点是
        让测试可以验证"自定义骨架"的链路而不必改全局常量
    """

    def __init__(
        self,
        *,
        get_toolset_tool_names: Callable[[Iterable[str]], list[str]],
        enabled_toolsets: list[str],
        memory_manager: Any,
        skill_loader: Optional[Any] = None,
        skeleton: str = SKELETON_PROMPT,
    ) -> None:
        self._get_toolset_tool_names = get_toolset_tool_names
        self._enabled_toolsets = list(enabled_toolsets)
        self._memory_manager = memory_manager
        self._skill_loader = skill_loader
        self._skeleton = skeleton

    # ── 段渲染（每个返回 str；空 str 表示该段跳过）────────────────────

    def _render_skeleton(self) -> str:
        """身份 / 风格 / 安全约束 — 永远第一段，永远不为空。"""
        return self._skeleton.strip()

    def _render_skill_index(self) -> str:
        """V21.3 占位：tier 1 skill 索引（仅 name + description）。

        V21.2 阶段 ``skill_loader is None`` → 始终返回空字符串。
        V21.3 实现时遍历 ``skill_loader.list_metadata()`` 渲染如：

            ## available skills
            - plan: Plan a refactor before touching code
            - test-driven-development: ...
        """
        if self._skill_loader is None:
            return ""
        # V21.3 will fill this in; signature 已就绪：
        try:
            metadata = list(self._skill_loader.list_metadata())
        except Exception:
            return ""
        if not metadata:
            return ""
        lines = ["## available skills"]
        for meta in metadata:
            lines.append(f"- {meta.name}: {meta.description}")
        return "\n".join(lines)

    def _render_memory_block(self) -> str:
        """复用 ``MemoryManager.build_system_prompt()`` —— 它内部自己处理空态。"""
        block = self._memory_manager.build_system_prompt()
        return (block or "").strip()

    def _render_tool_list(self) -> str:
        """工具列表段 — toolset 内置工具 + 所有 memory provider 暴露的工具。"""
        builtin_tool_names = list(self._get_toolset_tool_names(self._enabled_toolsets))
        provider_tool_names = list(self._memory_manager.get_all_tool_names())
        all_names = sorted(set(builtin_tool_names + provider_tool_names))
        if not all_names:
            return ""
        lines = ["## available tools"]
        for name in all_names:
            lines.append(f"- `{name}`")
        return "\n".join(lines)

    # ── 公共入口 ────────────────────────────────────────────────────

    def build(self) -> str:
        """渲染所有段并以单空行连接；空段自动跳过。"""
        sections = [
            self._render_skeleton(),
            self._render_skill_index(),
            self._render_memory_block(),
            self._render_tool_list(),
        ]
        return "\n\n".join(s for s in sections if s).strip()
