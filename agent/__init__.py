"""agent 子包 — V21 起：prompt builder / skill loader 等可复用模块。

- V21.2: prompt_builder.py — 三段式系统 prompt 拼装器
- V21.3（待开发）: skill_loader.py — progressive disclosure tier 1 加载器
"""

from agent.prompt_builder import PromptBuilder, SKELETON_PROMPT

__all__ = ["PromptBuilder", "SKELETON_PROMPT"]

