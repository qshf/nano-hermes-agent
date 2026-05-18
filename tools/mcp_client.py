"""
MCP Client — Agent 侧 MCP 连接管理。

简化版实现，对齐源项目核心流程：
- 后台 daemon 线程跑 asyncio event loop
- stdio 方式连接 MCP server 子进程
- list_tools() 发现工具 → 注册到 registry
- 监听 notifications/tools/list_changed → 自动刷新
- call_tool() 执行工具调用

使用方式：
    from tools.mcp_client import mcp_manager
    mcp_manager.connect("demo", "python", ["mcp_server_demo.py"])
    mcp_manager.call_tool("mcp_demo_get_weather", {"city": "北京"})
"""

import asyncio
import json
import threading
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tools.registry import registry

# --- 后台 event loop（与源项目 _ensure_mcp_loop 对齐）---

_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _run_loop_forever(loop: asyncio.AbstractEventLoop, ready: threading.Event):
    """在线程里运行 MCP 专用 event loop，并在 loop 真正开始跑时通知主线程。"""
    asyncio.set_event_loop(loop) # 把传进来的 loop 设为当前线程的 asyncio event loop。

    # call_soon 的回调会在 run_forever 启动后由 event loop 执行。
    # ready 被设置后，主线程再投递协程就不会撞上“loop 还没 running”的竞态。
    # 注意它不是立刻执行，而是放进 event loop 的任务队列里。
    # 等下面 run_forever() 开始跑以后，loop 会立刻执行它。
    loop.call_soon(ready.set) # 安排一个马上执行的回调：ready.set()。

    # 启动 event loop，并让它一直运行。
    # 它会一直等任务，比如后面主线程投递过来的：
    loop.run_forever()


def _ensure_mcp_loop():
    """启动后台 event loop 线程（daemon，进程退出时自动清理）。"""
    global _mcp_loop, _mcp_thread
    ready = threading.Event()

    with _lock:
        # 如果后台 loop 已经启动，就直接复用，避免重复创建线程。
        if _mcp_loop is not None and _mcp_loop.is_running():
            return

        # MCP 的 Python SDK 是 asyncio 风格；agent 主流程是同步交互式循环。
        # 所以这里单独开一个后台 event loop，专门运行 MCP 的异步连接和调用。
        _mcp_loop = asyncio.new_event_loop() #   创建 loop
        _mcp_thread = threading.Thread( # 创建后台线程
            target=_run_loop_forever, #  在线程里运行
            args=(_mcp_loop, ready),
            name="mcp-event-loop",
            # daemon=True 表示主程序退出时，这个后台线程不会阻止进程结束。
            daemon=True,
        )
        _mcp_thread.start() #  启动线程

    if not ready.wait(timeout=5):
        raise RuntimeError("MCP event loop failed to start")


def _run_on_mcp_loop(coro, timeout: float = 360):
    """在后台 loop 上调度协程，主线程同步等待结果。"""
    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        # 如果协程还没成功投递给 event loop，就手动关闭它。
        # 否则 Python 会提示 coroutine was never awaited。
        coro.close()
        raise RuntimeError("MCP event loop is not running")

    # 把一个 async 协程投递到后台 loop 里执行。
    # 返回的是 concurrent.futures.Future，主线程可以用 result() 等它完成。
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        coro.close()
        raise
    return future.result(timeout=timeout)


# --- MCP Server 连接管理 ---

class MCPConnection:
    """一个 MCP server 连接的生命周期管理。

    关键设计：整个 stdio_client context 在同一个 async task 里 enter 和 exit。
    disconnect 通过 asyncio.Event 通知该 task 退出，避免跨 task cancel scope 错误。
    """

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[ClientSession] = None
        self.registered_tools: list[str] = []
        # 用于通知持久 task 退出
        self._stop_event: Optional[asyncio.Event] = None
        # 持久 task 的引用
        self._task: Optional[asyncio.Task] = None

    async def start(self, command: str, args: list[str]):
        """启动持久 task，在其中完成连接、发现工具，然后等待 stop 信号。"""
        self._stop_event = asyncio.Event()
        ready = asyncio.Event()

        async def _run():
            server_params = StdioServerParameters(command=command, args=args)
            # stdio_client 和 ClientSession 的 enter/exit 都在这同一个 task 里
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self.session = session
                    ready.set()
                    # 阻塞等待 disconnect 信号
                    await self._stop_event.wait()
            # context 正常退出，子进程被清理

        self._task = asyncio.ensure_future(_run())
        # 等待连接就绪
        await ready.wait()

    async def discover_and_register(self):
        """发现工具并注册到 registry。"""
        result = await self.session.list_tools()

        for tool in result.tools:
            tool_name = f"mcp_{self.name}_{tool.name}"
            schema = {
                "name": tool_name,
                "description": f"[MCP:{self.name}] {tool.description or tool.name}",
                "parameters": tool.inputSchema,
            }
            handler = self._make_handler(tool.name)
            registry.register(schema, handler)
            self.registered_tools.append(tool_name)

    def _make_handler(self, server_tool_name: str):
        """为 MCP 工具生成 handler（通过后台 loop 调用 call_tool）。"""
        connection = self

        def handler(args: dict) -> str:
            async def _call():
                result = await connection.session.call_tool(server_tool_name, arguments=args)
                parts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                return "\n".join(parts) if parts else "{}"
            return _run_on_mcp_loop(_call())

        return handler

    async def refresh_tools(self):
        """刷新工具列表（对齐源项目 _refresh_tools）。"""
        for tool_name in self.registered_tools:
            registry.deregister(tool_name)
        self.registered_tools.clear()
        await self.discover_and_register()

    async def disconnect(self):
        """断开连接：通知持久 task 退出，等待 context 正常清理。"""
        for tool_name in self.registered_tools:
            registry.deregister(tool_name)
        self.registered_tools.clear()

        if self._stop_event:
            self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self.session = None


class MCPManager:
    """管理所有 MCP server 连接。"""

    def __init__(self):
        # key 是 server 别名，例如 "demo"；value 是具体连接对象。
        self._connections: dict[str, MCPConnection] = {}

    def connect(self, name: str, command: str, args: list[str]):
        """连接一个 MCP server（按需加载）。"""
        _ensure_mcp_loop()

        if name in self._connections:
            print(f"  [mcp] {name} already connected")
            return

        conn = MCPConnection(name)

        async def _do_connect():
            await conn.start(command, args)
            await conn.discover_and_register()

        _run_on_mcp_loop(_do_connect(), timeout=30)
        self._connections[name] = conn

    def disconnect(self, name: str) -> bool:
        """断开一个 MCP server。"""
        conn = self._connections.pop(name, None)
        if conn is None:
            return False
        _run_on_mcp_loop(conn.disconnect())
        return True

    def refresh(self, name: str):
        """刷新指定 server 的工具列表。"""
        conn = self._connections.get(name)
        if conn:
            _run_on_mcp_loop(conn.refresh_tools())

    def call_tool(self, tool_name: str, args: dict) -> str:
        """通过 registry dispatch 调用（handler 内部走 MCP loop）。"""
        return registry.dispatch(tool_name, args)

    @property
    def connected_servers(self) -> list[str]:
        return list(self._connections.keys())

    def get_tools(self, name: str) -> list[str]:
        conn = self._connections.get(name)
        return conn.registered_tools if conn else []


# 全局单例
mcp_manager = MCPManager()
