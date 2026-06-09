"""agent 子包 — V21 起：prompt builder / skill loader 等可复用模块。

- V21.2: prompt_builder.py — 三段式系统 prompt 拼装器
- V21.3: skill_loader.py — progressive disclosure tier 1 加载器
- V23.0: child_loop.py — 子 agent 同步 loop（多智能体最小切片）
- V26.4: voice_heartbeat.py — legacy 代码级语音心跳（不作为 v27.1 public surface）
- V27.1: external voice orchestrator envelope / phase lease host primitives
"""

from agent.prompt_builder import PromptBuilder, SKELETON_PROMPT
from agent.skill_loader import (
    SkillLoader,
    SkillMetadata,
    parse_frontmatter,
    skill_matches_platform,
)
from agent.child_loop import run_child_loop
from agent.runtime_phase import (
    PHASE_ASSISTANT_GENERATING_TEXT,
    PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS,
    PHASE_CHILD_AGENT_RUNNING,
    PHASE_TOOL_EXECUTING,
    PhaseSpan,
    PhaseTracker,
    tool_span,
)
from agent.turn_events import (
    PhasePreview,
    TextPreview,
    ToolPreview,
    TurnEventEnvelope,
    build_turn_event_envelope,
    preview_tool_result,
    preview_text,
    redact_text,
)
from agent.voice_orchestrator_client import (
    HttpVoiceOrchestratorClient,
    RecordingVoiceOrchestratorClient,
    VoiceEventSink,
    VoiceOrchestratorClient,
)

__all__ = [
    "PromptBuilder",
    "SKELETON_PROMPT",
    "SkillLoader",
    "SkillMetadata",
    "parse_frontmatter",
    "skill_matches_platform",
    "run_child_loop",
    "PHASE_ASSISTANT_GENERATING_TEXT",
    "PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS",
    "PHASE_CHILD_AGENT_RUNNING",
    "PHASE_TOOL_EXECUTING",
    "PhaseSpan",
    "PhaseTracker",
    "tool_span",
    "PhasePreview",
    "TextPreview",
    "ToolPreview",
    "TurnEventEnvelope",
    "build_turn_event_envelope",
    "preview_tool_result",
    "preview_text",
    "redact_text",
    "HttpVoiceOrchestratorClient",
    "RecordingVoiceOrchestratorClient",
    "VoiceEventSink",
    "VoiceOrchestratorClient",
]

