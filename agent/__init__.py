"""agent 子包 — V21 起：prompt builder / skill loader 等可复用模块。

- V21.2: prompt_builder.py — 三段式系统 prompt 拼装器
- V21.3: skill_loader.py — progressive disclosure tier 1 加载器
- V23.0: child_loop.py — 子 agent 同步 loop（多智能体最小切片）
- V26.4: voice_heartbeat.py — 代码级语音心跳（长任务防"挂机"感知）
"""

from agent.prompt_builder import PromptBuilder, SKELETON_PROMPT
from agent.skill_loader import (
    SkillLoader,
    SkillMetadata,
    parse_frontmatter,
    skill_matches_platform,
)
from agent.child_loop import run_child_loop
from agent.voice_heartbeat import VoiceHeartbeat

__all__ = [
    "PromptBuilder",
    "SKELETON_PROMPT",
    "SkillLoader",
    "SkillMetadata",
    "parse_frontmatter",
    "skill_matches_platform",
    "run_child_loop",
    "VoiceHeartbeat",
]


