"""
Nano Hermes Agent — V8: MemoryManager 编排器

架构变化（相比 V7）：
- 新增 MemoryManager：单一集成点，管理多个 provider
  - 工具路由（tool name → provider）
  - 错误隔离（一个 provider 失败不阻塞其他）
  - 一个外部 provider 限制（防止 schema 膨胀和后端冲突）
  - 上下文围栏辅助（sanitize_context / build_memory_context_block）
- agent.py 不再直接持有 provider，而是通过 manager 收 schema、路由 tool

运行方式：
    python agent.py
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry
from memory import BuiltinMemoryProvider, MemoryManager

# ─── 配置 ────────────────────────────────────────────────────────────────────
ENABLED_TOOLSETS = ["core"]

SYSTEM_PROMPT = """You are a helpful coding assistant. You have access to the following tools:
{tool_list}

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user.

{memory_block}"""

# V8: 通过 manager 编排 provider（目前只有 builtin，V10 会加外部）
memory_manager = MemoryManager()
memory_manager.add_provider(BuiltinMemoryProvider())
memory_manager.initialize_all()


def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    # 通过 manager 收集所有 provider 暴露的工具名
    provider_tool_names = list(memory_manager.get_all_tool_names())
    all_tool_names = sorted(set(tool_names + provider_tool_names))
    tool_list = "\n".join(f"- `{name}`" for name in all_tool_names)

    memory_block = memory_manager.build_system_prompt()

    return SYSTEM_PROMPT.format(tool_list=tool_list, memory_block=memory_block).strip()


def run_agent():
    load_dotenv()

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": build_system_prompt()}]

    # V8: 通过 manager 拿到内置 provider，用于 banner 和 /memory 命令
    builtin_provider = memory_manager.get_provider("builtin")

    print("=" * 60)
    print("  Nano Hermes Agent v8 — MemoryManager")
    print(f"  Model: {model}")
    print(f"  Toolsets: {ENABLED_TOOLSETS}")
    print(f"  Memory providers: {[p.name for p in memory_manager.providers]}")
    if builtin_provider is not None:
        store = builtin_provider.store
        print(f"    file: {store.file_path}")
        print(f"    entries: {len(store.entries)}, "
              f"usage: {store.char_count()}/{store.char_limit} chars")
    print(f"  Available tools: {', '.join(get_available_tool_names(ENABLED_TOOLSETS))}")
    print(f"  Memory tools: {', '.join(sorted(memory_manager.get_all_tool_names()))}")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

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

        messages.append({"role": "user", "content": user_input})

        while True:
            # 每轮重新计算：check_fn 结果可能变化（如用户中途装了 Docker）
            tools_schema = get_tool_definitions(ENABLED_TOOLSETS)
            # V8: 通过 manager 收集所有 provider 的 tool schema
            provider_schemas = memory_manager.get_all_tool_schemas()
            all_tools_schema = tools_schema + [
                {"type": "function", "function": s} for s in provider_schemas
            ]

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=all_tools_schema,
            )

            choice = response.choices[0]
            assistant_message = choice.message
            messages.append(assistant_message.model_dump())

            if not assistant_message.tool_calls:
                print(f"\nAgent > {assistant_message.content}\n")
                break

            for tool_call in assistant_message.tool_calls:
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


if __name__ == "__main__":
    run_agent()
