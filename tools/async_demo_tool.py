"""
Async Demo Tool — 演示 V4 异步桥接。

展示 is_async=True 的用法：handler 是 async def，
registry.dispatch() 自动通过 _run_async() 桥接到 sync 上下文。

对比 MCP 工具：
- MCP 需要自建 _run_on_mcp_loop 机制（连接是长生命周期，需要持久 event loop）
- V4 的 async 工具只需声明 is_async=True（一次性协程，用完即走）
"""

import asyncio
import json

from tools.registry import registry

ASYNC_DEMO_SCHEMA = {
    "name": "async_demo",
    "description": "异步演示工具：模拟一个耗时的网络请求（用 asyncio.sleep 模拟延迟）。",
    "parameters": {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "模拟等待的秒数（默认 1 秒）。",
            },
            "message": {
                "type": "string",
                "description": "完成后返回的消息。",
            },
        },
        "required": ["seconds"],
    },
}


async def async_demo_handler(args: dict) -> str:
    """异步 handler：await asyncio.sleep 模拟网络 I/O。"""
    seconds = args.get("seconds", 1)
    message = args.get("message", "async operation done")
    await asyncio.sleep(seconds)
    return json.dumps({
        "output": f"{message} (waited {seconds}s)",
    }, ensure_ascii=False)


registry.register(ASYNC_DEMO_SCHEMA, async_demo_handler, is_async=True)
