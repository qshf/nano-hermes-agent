"""
Nano Hermes Agent — V4: 类型修复 + 异步桥接

架构变化（相比 V3）：
- dispatch 前自动根据 JSON Schema 做参数类型强制转换（coerce）
- 注册时可声明 is_async=True，dispatch 自动桥接 async handler
- 新增 async_demo 工具演示异步桥接

运行方式：
    python agent.py
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry

# ─── 配置 ────────────────────────────────────────────────────────────────────
ENABLED_TOOLSETS = ["core"]

SYSTEM_PROMPT = """You are a helpful coding assistant. You have access to the following tools:
{tool_list}

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user."""


def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    tool_list = "\n".join(f"- `{name}`" for name in tool_names)
    return SYSTEM_PROMPT.format(tool_list=tool_list)


def run_agent():
    load_dotenv()

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": build_system_prompt()}]

    print("=" * 60)
    print("  Nano Hermes Agent v4 — 类型修复 + 异步桥接")
    print(f"  Model: {model}")
    print(f"  Toolsets: {ENABLED_TOOLSETS}")
    print(f"  Available tools: {', '.join(get_available_tool_names(ENABLED_TOOLSETS))}")
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

            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema,
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
