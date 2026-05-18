"""
MCP Demo Server — 最小 MCP 服务端（stdio 模式）。

提供 3 个工具：get_weather、get_time、write_content。
用于演示 agent 通过 /mcp 命令动态连接并发现工具。

运行方式（独立测试）：
    python mcp_server_demo.py

实际使用时由 agent 通过子进程自动启动。
"""

import json
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-server")


@mcp.tool()
def get_weather(city: str) -> str:
    """获取指定城市的天气信息（模拟数据）。"""
    weather_data = {
        "北京": {"temp": 22, "condition": "晴", "humidity": 45},
        "上海": {"temp": 24, "condition": "多云", "humidity": 72},
        "深圳": {"temp": 30, "condition": "阵雨", "humidity": 85},
    }
    data = weather_data.get(city, {"temp": 20, "condition": "未知", "humidity": 50})
    data["city"] = city
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def get_time(timezone_offset: int = 8) -> str:
    """获取指定时区的当前时间。timezone_offset 为 UTC 偏移小时数，默认 +8（北京时间）。"""
    tz = timezone(timedelta(hours=timezone_offset))
    now = datetime.now(tz)
    return json.dumps({
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": f"UTC{timezone_offset:+d}",
    }, ensure_ascii=False)


# @mcp.tool()
# def write_content(filepath: str, content: str, mode: str = "w") -> str:
#     """将内容写入指定文件。如果文件不存在会自动创建，存在则根据 mode 决定覆盖或追加。

#     Args:
#         filepath: 文件路径（绝对或相对路径）。
#         content: 要写入的内容。
#         mode: 写入模式，"w" 覆盖（默认），"a" 追加。
#     """
#     try:
#         with open(filepath, mode, encoding="utf-8") as f:
#             f.write(content)
#         return json.dumps({
#             "success": True,
#             "filepath": filepath,
#             "mode": mode,
#             "message": f"文件写入成功（{'覆盖' if mode == 'w' else '追加'}）",
#         }, ensure_ascii=False)
#     except Exception as e:
#         return json.dumps({
#             "success": False,
#             "filepath": filepath,
#             "error": str(e),
#         }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
