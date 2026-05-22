"""
Nano Hermes Agent — V17: Transport ABC + ChatCompletionsTransport

架构变化（相比 V16）：
- 新增 transports/ 子模块：ProviderTransport ABC + NormalizedResponse 数据类
- 新增 ChatCompletionsTransport：把 OpenAI 兼容协议从 agent.py 解耦
- 新增 transports.client_factory.make_llm_client：按 api_mode 实例化 SDK
- agent loop 调用点替换：build_kwargs → client.create(**kwargs) → normalize_response
- agent.py 不再 import openai，LLM 调用完全收敛进 transport 边界

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
    python agent.py
"""

import json
import os
import uuid

from dotenv import load_dotenv
load_dotenv()

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry
from memory import BuiltinMemoryProvider, MemoryManager, RemoteSemanticProvider
from context_compressor import ContextCompressor
from transports import get_transport
from transports.client_factory import make_llm_client

# ─── 配置 ────────────────────────────────────────────────────────────────────
ENABLED_TOOLSETS = ["core"]

SYSTEM_PROMPT = """You are a helpful coding assistant. You have access to the following tools:
{tool_list}

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user.

{memory_block}"""

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


def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    # 通过 manager 收集所有 provider 暴露的工具名
    provider_tool_names = list(memory_manager.get_all_tool_names())
    all_tool_names = sorted(set(tool_names + provider_tool_names))
    tool_list = "\n".join(f"- `{name}`" for name in all_tool_names)

    memory_block = memory_manager.build_system_prompt()

    return SYSTEM_PROMPT.format(tool_list=tool_list, memory_block=memory_block).strip()


def run_agent():


    # V17: 通过 transport 抽象层调用 LLM；client 由 factory 按 api_mode 实例化。
    # 当前唯一支持 "chat_completions"，V18 会增加 "anthropic_messages"。
    transport = get_transport("chat_completions")
    if transport is None:
        raise RuntimeError("chat_completions transport not registered")
    client = make_llm_client(transport.api_mode)
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": build_system_prompt()}]

    # V8: 通过 manager 拿到内置 provider，用于 banner 和 /memory 命令
    builtin_provider = memory_manager.get_provider("builtin")

    # V14: 跟踪当前 session_id
    current_session_id = os.environ.get("MEMORY_SESSION_ID", "default")

    # V15: 上下文压缩器
    compressor = ContextCompressor()

    print("=" * 60)
    print("  Nano Hermes Agent v17 — Transport ABC + ChatCompletionsTransport")
    print(f"  Model: {model}")
    print(f"  Transport: {transport.api_mode}")
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
    print("  Commands: /memory /tools /load /mcp /plugin /new /resume /session /compress")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

    turn_count = 0  # V9: 每轮递增，传给 on_turn_start

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

            # /memory 命令：查看当前记忆状态（通过 manager 找到 builtin provider）
            if user_input == "/memory":
                if builtin_provider is None:
                    print("  [memory] (no builtin provider)")
                    continue
                store = builtin_provider.store
                entries = store.entries
                if not entries:
                    print("  [memory] (empty)")
                else:
                    print(f"  [memory] {len(entries)} entries, "
                          f"{store.char_count()}/{store.char_limit} chars")
                    for i, entry in enumerate(entries, 1):
                        display = entry[:80] + "..." if len(entry) > 80 else entry
                        print(f"    {i}. {display}")
                continue

            # /load 命令：运行时动态加载 plugins/ 下的工具
            if user_input.startswith("/load "):
                filename = user_input[6:].strip()
                try:
                    from tools import load_plugin
                    old_gen = registry.generation
                    load_plugin(filename)
                    print(f"  [loaded] {filename} (generation: {old_gen} → {registry.generation})")
                    print(f"  [tools] {registry.tool_names}")
                except Exception as e:
                    print(f"  [error] {e}")
                continue

            # /mcp 命令：按需连接 MCP server
            if user_input.startswith("/mcp"):
                from tools.mcp_client import mcp_manager
                parts = user_input.split()

                if len(parts) == 1 or parts[1] == "list":
                    servers = mcp_manager.connected_servers
                    if not servers:
                        print("  [mcp] No connected servers. Use: /mcp connect <name> <command> [args...]")
                    else:
                        for s in servers:
                            tools = mcp_manager.get_tools(s)
                            print(f"  [mcp] {s}: {', '.join(tools)}")
                    continue

                if parts[1] == "connect" and len(parts) >= 4:
                    # /mcp connect mcp_server_demo python mcp_server_demo.py
                    name = parts[2]
                    command = parts[3]
                    args = parts[4:] if len(parts) > 4 else []
                    try:
                        old_gen = registry.generation
                        mcp_manager.connect(name, command, args)
                        tools = mcp_manager.get_tools(name)
                        print(f"  [mcp] Connected to '{name}' (generation: {old_gen} → {registry.generation})")
                        print(f"  [mcp] Tools: {', '.join(tools)}")
                    except Exception as e:
                        print(f"  [mcp error] {e}")
                    continue

                if parts[1] == "disconnect" and len(parts) >= 3:
                    name = parts[2]
                    old_gen = registry.generation
                    disconnected = mcp_manager.disconnect(name)
                    if disconnected:
                        print(f"  [mcp] Disconnected '{name}' (generation: {old_gen} → {registry.generation})")
                    else:
                        print(f"  [mcp] '{name}' is not connected. Use /mcp list to see connected servers.")
                    continue

                if parts[1] == "refresh" and len(parts) >= 3:
                    name = parts[2]
                    old_gen = registry.generation
                    mcp_manager.refresh(name)
                    tools = mcp_manager.get_tools(name)
                    print(f"  [mcp] Refreshed '{name}' (generation: {old_gen} → {registry.generation})")
                    print(f"  [mcp] Tools: {', '.join(tools)}")
                    continue

                print("  Usage:")
                print("    /mcp                          — list connected servers")
                print("    /mcp connect <name> <cmd> [args...]  — connect to MCP server")
                print("    /mcp disconnect <name>        — disconnect")
                print("    /mcp refresh <name>           — refresh tool list")
                continue

            # /plugin 命令：V5 插件生命周期管理
            if user_input.startswith("/plugin"):
                from tools import load_plugin, unload_plugin, list_plugins
                parts = user_input.split()

                if len(parts) == 1 or parts[1] == "list":
                    plugins = list_plugins()
                    if not plugins:
                        print("  [plugin] No loaded plugins. Use: /plugin load <filename>")
                    else:
                        for name, hooks in plugins.items():
                            print(f"  [plugin] {name}: {', '.join(hooks) if hooks else '(no hooks)'}")
                    continue

                if parts[1] == "load" and len(parts) >= 3:
                    filename = parts[2]
                    try:
                        load_plugin(filename)
                        plugins = list_plugins()
                        hooks = plugins.get(filename.replace(".py", "").replace(".py", ""), [])
                        print(f"  [plugin] Loaded '{filename}'")
                        print(f"  [plugin] Hooks: {', '.join(hooks) if hooks else '(none)'}")
                    except Exception as e:
                        print(f"  [plugin error] {e}")
                    continue

                if parts[1] == "unload" and len(parts) >= 3:
                    filename = parts[2]
                    if unload_plugin(filename):
                        print(f"  [plugin] Unloaded '{filename}'")
                    else:
                        print(f"  [plugin] '{filename}' is not loaded.")
                    continue

                print("  Usage:")
                print("    /plugin                  — list loaded plugins")
                print("    /plugin load <file>      — load plugin and register hooks")
                print("    /plugin unload <file>    — unload plugin and deregister hooks")
                continue

            # /tools 命令：查看当前可用工具
            if user_input == "/tools":
                available = get_available_tool_names(ENABLED_TOOLSETS)
                print(f"  [toolset] {ENABLED_TOOLSETS}")
                print(f"  [available] {', '.join(available)}")
                print(f"  [registered] {', '.join(registry.tool_names)}")
                continue

            # /session 命令：查看当前 session_id
            if user_input == "/session":
                print(f"  [session] {current_session_id}")
                continue

            # /compress 命令：手动触发上下文压缩（调试用）
            if user_input == "/compress":
                est = compressor.estimate_tokens(messages)
                print(f"  [compress] estimated tokens: {est}, threshold: {compressor.threshold_tokens}")
                if len(messages) < compressor.protect_first_n + 5:
                    print("  [compress] not enough messages to compress")
                    continue
                memory_manager.on_pre_compress_all(messages[compressor.protect_first_n:])
                messages = compressor.compress(messages, client, model)
                print(f"  [compress] compacted to {len(messages)} messages")
                continue

            # /new 命令：创建全新会话
            if user_input == "/new":
                new_id = f"session-{uuid.uuid4().hex[:8]}"
                memory_manager.on_session_switch_all(new_id, reset=True)
                current_session_id = new_id
                messages = [{"role": "system", "content": build_system_prompt()}]
                turn_count = 0
                print(f"  [session] New session: {current_session_id}")
                continue

            # /resume 命令：切回已有会话
            if user_input.startswith("/resume"):
                parts = user_input.split(maxsplit=1)
                if len(parts) < 2 or not parts[1].strip():
                    print("  Usage: /resume <session_id>")
                    continue
                target_id = parts[1].strip()
                memory_manager.on_session_switch_all(target_id, reset=False)
                current_session_id = target_id
                messages = [{"role": "system", "content": build_system_prompt()}]
                turn_count = 0
                print(f"  [session] Resumed: {current_session_id}")
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
                    messages = compressor.compress(messages, client, model)
                    print(f"  [compress] compacted to {len(messages)} messages")

                # 每轮重新计算：check_fn 结果可能变化（如用户中途装了 Docker）
                tools_schema = get_tool_definitions(ENABLED_TOOLSETS)
                # V8: 通过 manager 收集所有 provider 的 tool schema
                provider_schemas = memory_manager.get_all_tool_schemas()
                all_tools_schema = tools_schema + [
                    {"type": "function", "function": s} for s in provider_schemas
                ]

                response = client.chat.completions.create(
                    **transport.build_kwargs(
                        model=model,
                        messages=messages,
                        tools=all_tools_schema,
                    )
                )

                # V17: 通过 transport 把原生响应标准化成 NormalizedResponse。
                # 下游消费的 .content / .tool_calls / .usage 与原 OpenAI 字段同名，
                # ToolCall.function.name / .function.arguments 通过兼容 property 提供。
                if not transport.validate_response(response):
                    print("  [warn] invalid response shape, skipping turn")
                    break
                normalized = transport.normalize_response(response)

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
    finally:
        # V10: 释放外部 provider 的 httpx client；builtin 的 shutdown 是 no-op
        memory_manager.shutdown_all()


if __name__ == "__main__":
    run_agent()
