"""V27.1 重构 — 装配层（assembly），从 ``main.py`` 抽出。

为什么独立成文件
================
``main.py:run_agent`` 原本 558 行,前半段全是**装配**（日志 / transport / model /
runtime / voice sink / registry / memory / session 持久化 / compressor / skill /
prompt）,后半段才是 REPL + turn loop。装配和运行挤在一个函数 + 一堆 import-time
副作用 global（``memory_manager`` / ``skill_loader`` / ``prompt_builder`` 在 import
时就建好）里,是"胖 main"的根源。

本模块把**装配**收编:所有"建好就不再变"的运行期对象在 ``bootstrap_services()``
里一次性组装,打包成 ``AgentServices`` 返回。``main.py`` 退回瘦入口,``turn_loop.py``
拿 ``AgentServices`` 跑循环。纯结构搬运,零行为改动。

时序约束
========
- ``--cwd`` 的 ``os.chdir`` 必须在本模块任何"按 cwd 工作"的装配（PromptBuilder
  项目上下文段）之前完成 —— 由 ``main.py`` 在调用 ``bootstrap_services()`` 之前
  chdir 保证。
- ``setup_logging()`` 必须在 chain 构建 / 任何 transport 调用之前,否则早期 record
  收不到。本模块第一步就 setup。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from agent import PromptBuilder, SkillLoader, VoiceEventSink
from agent.env import env_bool
from agent.logging import get_logger, set_log_session, setup_logging
from agent.runtime import AgentRuntime, SESSION_TOKEN_KEYS
from agent.session_store import SessionStore
from context_compressor import ContextCompressor
from memory import BuiltinMemoryProvider, MemoryManager, RemoteSemanticProvider
from model_tools import get_available_tool_names
from tools.delegate_tool import DelegateContext, set_delegate_context as _inject_delegate_context
from tools.registry import registry
from tools.skill_view_tool import set_skill_loader as _inject_skill_loader
from transports.chain import build_chain_from_env
from transports.client_factory import make_llm_client
from transports.streaming import CancelToken

ENABLED_TOOLSETS = ["core"]

_HISTORY_PATH = Path.home() / ".nano_hermes_history"


@dataclass
class AgentServices:
    """``bootstrap_services()`` 的装配产物 —— 运行期所有"建好不再换"的对象。

    引用字段（turn loop 只读取,不替换）:
    - ``chain`` / ``client`` / ``model``   V19 TransportChain + 主家 client + 默认 model
    - ``runtime``                          父子共享的 AgentRuntime（stream / cancel / tokens）
    - ``cancel_token``                     ``runtime.cancel_token`` 的直引用(SIGINT/Esc 用)
    - ``agent_busy``                       覆盖整轮 tool loop 的 busy 标志（取消语境判定）
    - ``voice_event_sink``                 V27.1 外部语音 side-channel(可能 None)
    - ``memory_manager`` / ``builtin_provider``  V8 记忆编排 + 内置 provider
    - ``compressor``                       V15 上下文压缩器
    - ``session_store``                    V24 会话持久化
    - ``skill_loader`` / ``prompt_builder``  V21 skill + 三段式 system prompt
    - ``registry`` / ``enabled_toolsets``  工具注册表 + 启用的 toolset
    - ``input_session``                    V22 prompt_toolkit 输入框
    - ``log``                              已配好的 root logger

    装配出的对话起始状态（turn loop 会 mutate）:
    - ``messages``             system prompt + resume 历史
    - ``current_session_id``   已 resolve（含压缩链 tip 重定向）的会话 id
    - ``turn_count``           运行计数,**恒从 0 起**（resume 的轮号只进 banner,
                               对齐 main.py 原行为:resume 赋值后被无条件重置为 0）
    """

    chain: Any
    client: Any
    model: str
    runtime: AgentRuntime
    cancel_token: CancelToken
    agent_busy: threading.Event
    voice_event_sink: Optional[VoiceEventSink]
    memory_manager: MemoryManager
    builtin_provider: Any
    compressor: ContextCompressor
    session_store: SessionStore
    skill_loader: SkillLoader
    prompt_builder: PromptBuilder
    registry: Any
    enabled_toolsets: list[str]
    input_session: PromptSession
    log: Any
    messages: list[dict]
    current_session_id: str
    turn_count: int = 0


def build_input_session() -> PromptSession:
    """V22 输入框:prompt_toolkit 取代内建 input()（修 macOS libedit CJK 退格 +
    支持 Esc-Enter 多行 / IME / 历史上下键）。

    ``multiline=False`` + 自定义键绑定 = 默认 Enter 送出、Esc-Enter 插换行。
    Ctrl+C / Ctrl+D 仍抛 KeyboardInterrupt / EOFError(prompt_toolkit 自己拦,不走
    SIGINT handler),与 turn loop 的 try/except 兼容。
    """
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _insert_newline(event):
        # Esc-Enter（先按 Esc 松开再按 Enter）→ 缓冲里插 \n。macOS Terminal.app 下
        # Option-Enter 不发 Esc 序列;要 Option-Enter 自行去 Terminal 设置勾 Meta key。
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(_HISTORY_PATH)),
        key_bindings=kb,
        multiline=False,
    )


def build_memory_manager() -> MemoryManager:
    """V8/V10/V11:建 MemoryManager —— builtin provider 必挂,remote 按 env 选挂。

    ``MEMORY_SERVICE_URL`` 设了才注册 RemoteSemanticProvider;探 /healthz 不通则
    打 warning 跳过(不阻断启动)。
    """
    manager = MemoryManager()
    manager.add_provider(BuiltinMemoryProvider())

    remote_url = os.environ.get("MEMORY_SERVICE_URL", "").strip()
    if remote_url:
        tags_raw = os.environ.get("MEMORY_RETAIN_TAGS", "").strip()
        retain_tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        remote = RemoteSemanticProvider(
            base_url=remote_url,
            bank_id=os.environ.get("MEMORY_BANK_ID", "hermes"),
            budget=os.environ.get("MEMORY_RECALL_BUDGET", "mid"),
            memory_mode=os.environ.get("MEMORY_MODE", "hybrid"),
            prefetch_method=os.environ.get("MEMORY_PREFETCH_METHOD", "recall"),
            auto_retain=env_bool("MEMORY_AUTO_RETAIN", True),
            auto_recall=env_bool("MEMORY_AUTO_RECALL", True),
            retain_tags=retain_tags,
            retain_every_n_turns=max(1, int(os.environ.get("MEMORY_RETAIN_EVERY_N_TURNS", "1"))),
        )
        if remote.is_available():
            manager.add_provider(remote)
        else:
            print(f"  [warn] MEMORY_SERVICE_URL set but {remote_url}/healthz unreachable — skipping")
            remote.shutdown()

    manager.initialize_all(session_id=os.environ.get("MEMORY_SESSION_ID", "default"))
    return manager


def build_prompt_stack(memory_manager: MemoryManager) -> tuple[SkillLoader, PromptBuilder]:
    """V21.2/V21.3:建 skill loader + 三段式 PromptBuilder。

    skill_loader 同时注入 skill_view_tool(tier 2 工具回调)和 PromptBuilder(tier 1
    索引段),两条路径共享同一份 metadata 缓存。PromptBuilder 的 cwd 在调用时捕获
    —— 调用方须保证 ``--cwd`` chdir 已完成(项目上下文段据此找 AGENTS.md)。
    """
    skill_loader = SkillLoader(Path(__file__).resolve().parent.parent / "skills")
    skill_loader.scan()
    _inject_skill_loader(skill_loader)

    prompt_builder = PromptBuilder(
        get_toolset_tool_names=get_available_tool_names,
        enabled_toolsets=ENABLED_TOOLSETS,
        memory_manager=memory_manager,
        skill_loader=skill_loader,
        cwd=Path.cwd(),
    )
    return skill_loader, prompt_builder


def build_transport() -> tuple[Any, Any, str]:
    """V19:建 transport chain + 解析主家 client / 默认 model。

    ``TRANSPORT_CHAIN`` 优先(多家),否则退化 ``TRANSPORT_MODE`` 单家。model 优先取
    chain 主家内联模型名,为 None(legacy 单家未内联)才回退 MODEL env —— 修 v24
    把假 model 名写进 sessions 表的 bug(详见 decisions/v25.1.md 决策 8)。
    """
    chain_env = os.environ.get("TRANSPORT_CHAIN", "").strip()
    if not chain_env:
        chain_env = os.environ.get("TRANSPORT_MODE", "chat_completions")

    chain = build_chain_from_env(
        chain_env,
        client_factory=make_llm_client,
        failure_threshold=int(os.environ.get("FAILOVER_FAILURE_THRESHOLD", "3")),
        cooldown_seconds=float(os.environ.get("FAILOVER_COOLDOWN_SECONDS", "60")),
        max_retries=int(os.environ.get("FAILOVER_MAX_RETRIES", "2")),
        base_delay=float(os.environ.get("FAILOVER_BASE_DELAY", "1.0")),
        cache_enabled=env_bool("PROMPT_CACHE_ENABLED", False),
        cache_ttl=os.environ.get("PROMPT_CACHE_TTL", "5m"),
    )
    client = chain.primary_client
    model = chain.primary_model or os.environ.get("MODEL", "gpt-4o-mini")
    return chain, client, model


def _resume_session(session_store: SessionStore, runtime: AgentRuntime, messages: list[dict]) -> tuple[str, str, int]:
    """V24:解析 resume tip + 挂回历史。返回 (session_id, resumed_note, resumed_turn)。

    ``resolve_resume_tip`` 先把"曾被压缩分裂成链 root"的 session 重定向到最新 tip,
    避免重载压缩前超长 root 后下一轮立刻重压(v24.1)。命中存档则 extend messages、
    恢复 session_tokens,并回报存档里的轮号(仅用于 banner 展示)。
    """
    current_session_id = os.environ.get("MEMORY_SESSION_ID", "default")
    current_session_id = session_store.resolve_resume_tip(current_session_id)
    resumed_note = "new"
    resumed_turn = 0
    saved = session_store.load(current_session_id)
    if saved is not None and saved["messages"]:
        messages.extend(saved["messages"])
        resumed_turn = saved["turn_count"]
        for k in SESSION_TOKEN_KEYS:
            runtime.session_tokens[k] = saved["session_tokens"].get(k, 0)
        resumed_note = f"resumed {len(saved['messages'])} msgs, turn {resumed_turn}"
    return current_session_id, resumed_note, resumed_turn


def _print_banner(svc: "AgentServices", resumed_note: str) -> None:
    """开机 banner —— 纯展示,从 run_agent 原样搬来(行为不变)。"""
    chain, model = svc.chain, svc.model
    print("=" * 60)
    print("  Nano Hermes Agent v23.4 — 多智能体结构化结果 + 父子成本聚合")
    print(f"  Default MODEL (entries 不内联时回退到此): {model}")
    chain_modes = " → ".join(
        f"{e.api_mode}({e.model})" if e.model else e.api_mode
        for e in chain.entries
    )
    print(f"  Transport chain: {chain_modes}")
    print(f"    failure_threshold={chain.failure_threshold}, "
          f"cooldown={chain.cooldown_seconds}s, "
          f"max_retries={chain.max_retries}, base_delay={chain.base_delay}s")
    if chain.cache_enabled:
        print(f"    prompt_cache: enabled (ttl={chain.cache_ttl}, "
              f"strategy=system_and_3, applies to anthropic_messages only)")
    else:
        print(f"    prompt_cache: disabled (set PROMPT_CACHE_ENABLED=1 to enable)")
    print(f"  Streaming: {'on' if svc.runtime.stream_enabled else 'off'} (toggle: /stream on|off)")
    print(f"  Session: {svc.current_session_id} ({resumed_note})")
    print(f"  Toolsets: {svc.enabled_toolsets}")
    print(f"  Memory providers: {[p.name for p in svc.memory_manager.providers]}")
    if svc.builtin_provider is not None:
        store = svc.builtin_provider.store
        print(f"    file: {store.file_path}")
        print(f"    entries: {len(store.entries)}, "
              f"usage: {store.char_count()}/{store.char_limit} chars")
    print(f"  Compression: threshold={svc.compressor.threshold_tokens}tok, "
          f"tail_budget={svc.compressor.tail_token_budget}tok, "
          f"protect_head={svc.compressor.protect_first_n}")
    print(f"  Available tools: {', '.join(get_available_tool_names(svc.enabled_toolsets))}")
    print(f"  Memory tools: {', '.join(sorted(svc.memory_manager.get_all_tool_names()))}")
    skill_names = svc.skill_loader.names()
    if skill_names:
        print(f"  Skills: {len(skill_names)} loaded ({', '.join(skill_names)})")
    else:
        print(f"  Skills: 0 loaded (skills/ empty or absent)")
    cwd_now = Path.cwd()
    if env_bool("NANO_IGNORE_RULES", False):
        print(f"  Working dir: {cwd_now} (project context: skipped via NANO_IGNORE_RULES)")
    else:
        from agent.prompt_builder import PROJECT_CONTEXT_FILE_NAMES as _PC_NAMES
        hit = next((n for n in _PC_NAMES if (cwd_now / n).is_file()), None)
        if hit:
            print(f"  Working dir: {cwd_now} (project context: {hit})")
        else:
            print(f"  Working dir: {cwd_now} (project context: none — tried {', '.join(_PC_NAMES)})")
    print("  Commands: /help to list all (V21.1: dispatched via cli registry)")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()


def connect_mcp_servers(log: Any) -> None:
    """V27.2 (FR-4): 启动期挂载外部 MCP 服务（按需，env 门控）。

    控制平面与观测平面正交（见 docs/voice-system/multi-service-focus-rotation-plan）：
    nano 通过 ``mcp_manager.connect`` 把外部 agent 服务当工具调（阻塞请求/响应），
    服务内部进度由它**自己** POST 给 voice orchestrator —— nano 不转发子细节，契合
    v27.1 非目标第 3 条「父进程只发 aggregate child phase」。

    env ``NANO_MCP_SERVERS`` 形如 ``search=python:/abs/fake_search_service.py``，
    分号隔多条；``connect`` 后 registry 自动注册 ``mcp_<name>_<tool>``，主 agent 可调。
    连接失败只 warning 不掀翻启动（外部服务是可选增强，不该阻断主 agent）。
    """
    raw = os.environ.get("NANO_MCP_SERVERS", "").strip()
    if not raw:
        return
    from tools.mcp_client import mcp_manager

    for spec in raw.split(";"):
        spec = spec.strip()
        if not spec or "=" not in spec:
            continue
        name, rhs = spec.split("=", 1)
        name, rhs = name.strip(), rhs.strip()
        command, _, arg = rhs.partition(":")
        command, arg = command.strip(), arg.strip()
        if not (name and command and arg):
            log.warning("skip malformed NANO_MCP_SERVERS entry: %r", spec)
            continue
        try:
            mcp_manager.connect(name, command, [arg])
            log.info("mcp connected: %s (%s %s) → tools=%s",
                     name, command, arg, mcp_manager.get_tools(name))
        except Exception as exc:  # noqa: BLE001 - 外部服务可选，连不上不阻断启动
            log.warning("mcp connect failed for %s: %s: %s", name, type(exc).__name__, exc)


def bootstrap_services() -> AgentServices:
    """组装运行期全部"建好不再换"的对象,返回 ``AgentServices``。

    顺序硬约束:setup_logging 先行(早期 record 收得到)→ transport → runtime →
    voice sink → registry/delegate 注入 → memory → prompt/session。调用方须在
    本函数之前完成 ``--cwd`` chdir(PromptBuilder 项目上下文段依赖 cwd)。
    """
    setup_logging()
    log = get_logger()

    chain, client, model = build_transport()
    log.info(
        "agent boot: model=%s chain=[%s] failover(threshold=%s cooldown=%ss)",
        model,
        ",".join(f"{e.api_mode}:{e.model or '-'}" for e in chain.entries),
        chain.failure_threshold,
        chain.cooldown_seconds,
    )

    stream_enabled = env_bool("STREAM_ENABLED", True)
    cancel_token = CancelToken()
    runtime = AgentRuntime(stream_enabled=stream_enabled, cancel_token=cancel_token)

    voice_event_sink = VoiceEventSink.create_from_env()
    if voice_event_sink is not None:
        runtime.voice_event_sink = voice_event_sink
        voice_event_sink.start()
        log.info("voice orchestrator sink on")

    registry.set_runtime(runtime)
    agent_busy = threading.Event()

    memory_manager = build_memory_manager()

    # V27.2 (FR-4) / V27.4 (C): 挂外部 MCP 服务（env 门控）必须在算
    # parent_full_toolset 快照**之前** —— 子 agent 的 _resolve_child_toolset 读的是
    # 这份冻结快照（delegate_tool.py），connect 晚于快照会让 mcp_* 永远进不了子允许集
    # （主 agent 读 registry 实时态，不受影响）。connect 只依赖 registry（line 347
    # set_runtime 已先行）+ os.environ，此处依赖已齐备。
    connect_mcp_servers(log)

    # delegate 注入:父全集 = registry 注册的 + memory 暴露的;黑名单由
    # _resolve_child_toolset 自己过滤。子与父共享 runtime（同一 cancel_token,
    # stream_enabled 动态读取,让 /stream on|off 同时影响父子）。
    parent_full_toolset = sorted(
        set(registry.tool_names) | set(memory_manager.get_all_tool_names())
    )
    _inject_delegate_context(DelegateContext(
        chain=chain,
        model=model,
        parent_toolset_names=set(parent_full_toolset),
        runtime=runtime,
    ))

    skill_loader, prompt_builder = build_prompt_stack(memory_manager)
    builtin_provider = memory_manager.get_provider("builtin")

    compressor = ContextCompressor()
    session_store = SessionStore()

    messages: list[dict] = [{"role": "system", "content": prompt_builder.build()}]
    current_session_id, resumed_note, _resumed_turn = _resume_session(
        session_store, runtime, messages
    )
    set_log_session(current_session_id)
    log.info("session ready: %s (%s)", current_session_id, resumed_note)

    services = AgentServices(
        chain=chain,
        client=client,
        model=model,
        runtime=runtime,
        cancel_token=cancel_token,
        agent_busy=agent_busy,
        voice_event_sink=voice_event_sink,
        memory_manager=memory_manager,
        builtin_provider=builtin_provider,
        compressor=compressor,
        session_store=session_store,
        skill_loader=skill_loader,
        prompt_builder=prompt_builder,
        registry=registry,
        enabled_toolsets=ENABLED_TOOLSETS,
        input_session=build_input_session(),
        log=log,
        messages=messages,
        current_session_id=current_session_id,
        # turn_count 恒 0 起 —— 对齐 main.py 原行为(resume 轮号只进 banner)
        turn_count=0,
    )
    _print_banner(services, resumed_note)
    return services
