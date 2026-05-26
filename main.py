"""
Nano Hermes Agent — V21.2: Prompt Builder 三段式

V21 主题是"清算 V4 阶段交互层债务"，按 v21.1/v21.2/v21.3 三档落地：
- V21.1（已完成）：slash 命令拆出到 ``cli/commands/<name>.py``，主循环只
  调 ``cli.dispatch(line, ctx)``；agent.py 整体重命名为 ``main.py`` 让出
  ``agent/`` 子包名给 V21.2 的 prompt_builder 和 V21.3 的 skill_loader。
- V21.2（本档）：``agent/prompt_builder.py`` 三段式 — 骨架 + skill 索引段
  （V21.3 填充）+ memory 块 + 工具列表段。删掉 V4 起的
  ``SYSTEM_PROMPT.format(...)`` 模板替换。
- V21.3（待开发）：``agent/skill_loader.py`` + ``tools/skill_view_tool.py``
  + ``skills/<name>/SKILL.md`` 示例 + ``/skill`` 命令。

V20 架构（向下保留）：
- transports/prompt_caching.py：``apply_anthropic_cache_control`` (system_and_3 策略)
- ABC hook ``apply_prompt_cache``：默认 identity，AnthropicTransport 重写
- ChatCompletionsTransport / AnthropicTransport 实现 ``extract_cache_stats``
- chain 在 ``_try_with_retry`` 内做注入 + 累计 cache 命中率到 entry
- ``Usage`` 增加 ``cache_creation_tokens`` 字段，区分 read/write
- ``/transport`` 命令展示 read/write/uncached/hit_rate

env 开关（V20 新增）：
    PROMPT_CACHE_ENABLED        1/0（默认 0，启用后链上每个 entry 自决定是否打标记）
    PROMPT_CACHE_TTL            5m（默认）/ 1h（1h 单价更高但 TTL 长）

env 开关（V19）：
    TRANSPORT_CHAIN              chat_completions,anthropic_messages（不设则用 TRANSPORT_MODE 单家）
    FAILOVER_FAILURE_THRESHOLD   断路器打开阈值（默认 3 次连续失败）
    FAILOVER_COOLDOWN_SECONDS    断路器冷却时间（默认 60s）
    FAILOVER_MAX_RETRIES         单 transport RETRYABLE 错误最大重试次数（默认 2）
    FAILOVER_BASE_DELAY          backoff 基数（默认 1.0s）

env 开关（V18 保留）：
    TRANSPORT_MODE              chat_completions（默认）/ anthropic_messages
    ANTHROPIC_API_KEY           Anthropic SDK key（DashScope 等）
    ANTHROPIC_BASE_URL          Anthropic SDK base_url（如 https://dashscope.aliyuncs.com/apps/anthropic）

env 开关：
    CONTEXT_WINDOW              模型上下文窗口大小（默认 32000 tokens）
    CONTEXT_THRESHOLD_PERCENT   压缩阈值比例（默认 0.75）
    CONTEXT_PROTECT_HEAD        保留前 N 条消息（默认 3）
    CONTEXT_TAIL_BUDGET         tail 保留 token 预算（默认 4000）
    MEMORY_SERVICE_URL          外部记忆服务 URL（设了才挂 remote_semantic）
    MEMORY_SESSION_ID           会话隔离 id（默认 default）
    MEMORY_BANK_ID              bank 命名空间（默认 hermes）
    MEMORY_MODE                 context / tools / hybrid（默认 hybrid）
    MEMORY_PREFETCH_METHOD      recall / reflect（默认 recall）
    MEMORY_RECALL_BUDGET        low / mid / high（默认 mid → k=5）
    MEMORY_AUTO_RETAIN          1/0（默认 1）
    MEMORY_AUTO_RECALL          1/0（默认 1）
    MEMORY_RETAIN_TAGS          逗号分隔的 tags（默认空）

启动顺序（另开终端）：
    docker compose down -v && docker compose up -d   # 重建 DB（schema 变了）
    python scripts/mock_memory_server.py
    export MEMORY_SERVICE_URL=http://127.0.0.1:8765
    python main.py
"""

import json
import os

from dotenv import load_dotenv
load_dotenv(override=True)

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry
from memory import BuiltinMemoryProvider, MemoryManager, RemoteSemanticProvider
from context_compressor import ContextCompressor
from transports.chain import FailoverExhausted, build_chain_from_env
from transports.client_factory import make_llm_client
from agent import PromptBuilder
import cli  # 触发 cli/commands 下所有命令的装饰器注册

# ─── 配置 ────────────────────────────────────────────────────────────────────
ENABLED_TOOLSETS = ["core"]

# V8: 通过 manager 编排 provider；V10: 按环境变量加挂外部 provider
memory_manager = MemoryManager()
memory_manager.add_provider(BuiltinMemoryProvider())

# V10/V10.1: 按需注册远端语义记忆 provider（mock 服务见 scripts/mock_memory_server.py）
# V11: 支持 memory_mode / prefetch_method / bank_id / retain_tags 配置
_remote_url = os.environ.get("MEMORY_SERVICE_URL", "").strip()
if _remote_url:
    _tags_raw = os.environ.get("MEMORY_RETAIN_TAGS", "").strip()
    _retain_tags = [t.strip() for t in _tags_raw.split(",") if t.strip()] if _tags_raw else []

    _remote = RemoteSemanticProvider(
        base_url=_remote_url,
        bank_id=os.environ.get("MEMORY_BANK_ID", "hermes"),
        budget=os.environ.get("MEMORY_RECALL_BUDGET", "mid"),
        memory_mode=os.environ.get("MEMORY_MODE", "hybrid"),
        prefetch_method=os.environ.get("MEMORY_PREFETCH_METHOD", "recall"),
        auto_retain=os.environ.get("MEMORY_AUTO_RETAIN", "1") not in ("0", "false", "False", ""),
        auto_recall=os.environ.get("MEMORY_AUTO_RECALL", "1") not in ("0", "false", "False", ""),
        retain_tags=_retain_tags,
        retain_every_n_turns=max(1, int(os.environ.get("MEMORY_RETAIN_EVERY_N_TURNS", "1"))),
    )
    if _remote.is_available():
        memory_manager.add_provider(_remote)
    else:
        print(f"  [warn] MEMORY_SERVICE_URL set but {_remote_url}/healthz unreachable — skipping")
        _remote.shutdown()

memory_manager.initialize_all(session_id=os.environ.get("MEMORY_SESSION_ID", "default"))


# V21.2: 三段式 PromptBuilder 替换 V4 的 SYSTEM_PROMPT.format(...)
# 段顺序固定（骨架 → skill 索引 → memory → 工具列表），空段自动跳过；
# V21.3 把 SkillLoader 注入进来即可填充 tier 1 索引段。
prompt_builder = PromptBuilder(
    get_toolset_tool_names=get_available_tool_names,
    enabled_toolsets=ENABLED_TOOLSETS,
    memory_manager=memory_manager,
    skill_loader=None,   # V21.3 落地后改为 SkillLoader 实例
)


def build_system_prompt() -> str:
    """V4→V21.1 兼容入口；V21.2 起 thin wrapper 委托给 prompt_builder.build()。

    保留模块函数是为了兼容 V21.1 期间 ``AgentCtx.build_system_prompt`` 字段
    的 callable 类型 — 单测里 mock ctx 时直接传 ``lambda: "..."`` 即可。
    """
    return prompt_builder.build()


def run_agent():


    # V19: 构建 transport 链 — TRANSPORT_CHAIN 优先（多家），否则退化为
    # TRANSPORT_MODE 单家（行为同 V18）。chain 长度 1 时仍带 RETRYABLE 错误重试，
    # 但不会切换 — 链长 ≥ 2 才有 failover 价值。
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
        cache_enabled=os.environ.get("PROMPT_CACHE_ENABLED", "0") not in ("0", "false", "False", ""),
        cache_ttl=os.environ.get("PROMPT_CACHE_TTL", "5m"),
    )

    # 兼容存量代码路径 — 主家用于 client 引用（仅作为 compressor.compress 第三个
    # 位置参数兼容签名传入；真正的 LLM 调用统一走 chain.call()）。
    client = chain.primary_client
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": build_system_prompt()}]

    # V8: 通过 manager 拿到内置 provider，用于 banner 和 /memory 命令
    builtin_provider = memory_manager.get_provider("builtin")

    # V14: 跟踪当前 session_id
    current_session_id = os.environ.get("MEMORY_SESSION_ID", "default")

    # V15: 上下文压缩器
    compressor = ContextCompressor()

    print("=" * 60)
    print("  Nano Hermes Agent v21.2 — Prompt Builder 三段式")
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
    print(f"  Session: {current_session_id}")
    print(f"  Toolsets: {ENABLED_TOOLSETS}")
    print(f"  Memory providers: {[p.name for p in memory_manager.providers]}")
    if builtin_provider is not None:
        store = builtin_provider.store
        print(f"    file: {store.file_path}")
        print(f"    entries: {len(store.entries)}, "
              f"usage: {store.char_count()}/{store.char_limit} chars")
    print(f"  Compression: threshold={compressor.threshold_tokens}tok, "
          f"tail_budget={compressor.tail_token_budget}tok, "
          f"protect_head={compressor.protect_first_n}")
    print(f"  Available tools: {', '.join(get_available_tool_names(ENABLED_TOOLSETS))}")
    print(f"  Memory tools: {', '.join(sorted(memory_manager.get_all_tool_names()))}")
    print("  Commands: /help to list all (V21.1: dispatched via cli registry)")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

    turn_count = 0  # V9: 每轮递增，传给 on_turn_start

    # V21.1: slash handler 共享的运行期上下文
    ctx = cli.AgentCtx(
        messages=messages,
        current_session_id=current_session_id,
        turn_count=turn_count,
        chain=chain,
        client=client,
        model=model,
        memory_manager=memory_manager,
        builtin_provider=builtin_provider,
        compressor=compressor,
        registry=registry,
        enabled_toolsets=ENABLED_TOOLSETS,
        build_system_prompt=build_system_prompt,
        prompt_builder=prompt_builder,
    )

    try:
        while True:
            try:
                user_input = input("You > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break

            # V21.1: slash 命令统一通过 cli.dispatch 路由到 cli/commands/*.py
            # handler 通过 ctx 直接 mutate 状态：
            #   - messages 与 ctx.messages 共享同一 list 引用（in-place ops）
            #   - session_id / turn_count 由 handler 改 ctx.* 后此处同步回局部
            if cli.dispatch(user_input, ctx):
                current_session_id = ctx.current_session_id
                turn_count = ctx.turn_count
                continue

            # V9 生命周期：每轮开始通知 + prefetch 召回
            # 在 user message 入队前完成，prefetch 结果用围栏包裹后注入
            memory_manager.on_turn_start_all(turn_count, user_input)
            recalled = memory_manager.prefetch_all(user_input)
            turn_count += 1

            # prefetch 结果与原始 user 输入合并到同一条 user message
            # 注入位置选 user message（不是 system prompt）有两个原因：
            #   1. 召回内容每轮不同，放进 system prompt 会破坏前缀缓存
            #   2. 围栏 + 系统注释明确告诉模型"这是召回内容，不是新输入"
            if recalled:
                user_message_content = f"{recalled}\n\n{user_input}"
            else:
                user_message_content = user_input

            messages.append({"role": "user", "content": user_message_content})

            # 收集本轮 assistant 的最终文本响应（不含 tool call），用于 sync
            final_assistant_text = ""

            while True:
                # V15: 压缩检查 — API 调用前判断是否需要压缩上下文
                if compressor.should_compress(messages):
                    print("  [compress] context exceeds threshold, compacting...")
                    head_end = compressor.protect_first_n
                    memory_manager.on_pre_compress_all(messages[head_end:])
                    # V19: 摘要 LLM 调用也走 chain，享受 failover
                    # V21.1: 用 in-place 替换避免局部 messages 与 ctx.messages 引用分歧
                    compacted = compressor.compress(messages, client, model, transport=chain)
                    messages[:] = compacted
                    print(f"  [compress] compacted to {len(messages)} messages")

                # 每轮重新计算：check_fn 结果可能变化（如用户中途装了 Docker）
                tools_schema = get_tool_definitions(ENABLED_TOOLSETS)
                # V8: 通过 manager 收集所有 provider 的 tool schema
                provider_schemas = memory_manager.get_all_tool_schemas()
                all_tools_schema = tools_schema + [
                    {"type": "function", "function": s} for s in provider_schemas
                ]

                # V19: 统一 LLM 调用走 chain — 主家失败自动切备家。
                # ValueError 仍按"响应不合法"处理，FailoverExhausted 表示链全挂。
                try:
                    normalized = chain.call(
                        model=model,
                        messages=messages,
                        tools=all_tools_schema,
                    )
                except FailoverExhausted as e:
                    print(f"  [error] all transports failed: {e}")
                    break
                except ValueError:
                    print("  [warn] invalid response shape, skipping turn")
                    break

                # 用 API 返回的真实 token 数更新压缩器（下一轮触发判断用）
                if normalized.usage and normalized.usage.prompt_tokens:
                    '''
                    prompt_tokens — 输入（所有 messages + tools schema）的 token 数
                    completion_tokens — 本次 assistant 生成的 token 数
                    total_tokens — 两者之和
                    '''
                    compressor.update_usage(normalized.usage.prompt_tokens)

                # 把标准化响应回填进对话历史（保持 OpenAI 消息 shape，下一轮 build_kwargs 还能消费）
                assistant_dump: dict = {"role": "assistant", "content": normalized.content}
                if normalized.tool_calls:
                    assistant_dump["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in normalized.tool_calls
                    ]
                # DeepSeek/Kimi/Moonshot thinking mode 要求每条 assistant 消息回传 reasoning_content
                # 否则下一轮请求 400: "The reasoning_content in the thinking mode must be passed back"
                # 对齐源项目 run_agent.py:9621-9635（pad 一个空格避开 DeepSeek V4 Pro 的非空校验）
                rc = normalized.reasoning_content
                if rc is not None:
                    assistant_dump["reasoning_content"] = rc
                elif normalized.tool_calls:
                    assistant_dump["reasoning_content"] = " "
                messages.append(assistant_dump)

                if not normalized.tool_calls:
                    final_assistant_text = normalized.content or ""
                    print(f"\nAgent > {final_assistant_text}\n")
                    break

                for tool_call in normalized.tool_calls:
                    name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments)
                    print(f"  [tool] {name}({tool_call.function.arguments})")

                    # V8: 路由 — manager 接管的工具走 manager，其余走 registry
                    if memory_manager.has_tool(name):
                        result = memory_manager.handle_tool_call(name, args)
                    else:
                        result = registry.dispatch(name, args)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

                    try:
                        parsed = json.loads(result)
                        if "error" in parsed:
                            print(f"  [error] {parsed['error']}")
                        elif "output" in parsed:
                            output = parsed["output"]
                            if len(output) > 200:
                                output = output[:200] + "..."
                            print(f"  [result] {output}")
                        else:
                            print(f"  [result] (ok)")
                    except json.JSONDecodeError:
                        print(f"  [result] {result[:100]}")

            # V9 生命周期：tool loop 结束后持久化对话
            # sync 用原始 user 输入（不含围栏），保持后端记录干净
            memory_manager.sync_all(user_input, final_assistant_text)

            # V13: 预热下一轮的 recall — 用当轮 user input 作为 query
            memory_manager.queue_prefetch_all(user_input)

            # V21.1: 同步本轮 turn_count 到 ctx（messages / session_id 已通过引用共享）
            ctx.turn_count = turn_count
    finally:
        # V10: 释放外部 provider 的 httpx client；builtin 的 shutdown 是 no-op
        memory_manager.shutdown_all()


if __name__ == "__main__":
    run_agent()
