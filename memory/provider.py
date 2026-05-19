"""
MemoryProvider ABC — 记忆后端的统一契约。

V7 核心：定义任何记忆后端必须满足的接口。
将"工具接口 + 存储逻辑 + 生命周期"三个关注点解耦。

核心方法：
- name: 短标识符
- is_available(): 是否就绪
- initialize(): 每会话初始化
- get_tool_schemas(): 暴露给模型的工具 schema
- handle_tool_call(): 分发工具调用
- system_prompt_block(): 注入 system prompt 的静态文本
- shutdown(): 清理退出
"""

from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """记忆后端抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """短标识符，如 'builtin', 'semantic'。"""

    @abstractmethod
    def is_available(self) -> bool:
        """是否就绪（不做网络调用，只检查配置和依赖）。"""

    @abstractmethod
    def initialize(self, session_id: str = "", **kwargs) -> None:
        """每会话一次性初始化。建立连接、创建资源等。"""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """返回 OpenAI function calling 格式的 tool schema 列表。

        无工具的 provider 返回空列表。
        """

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """分发工具调用，返回 JSON 字符串。

        只会收到 get_tool_schemas() 中声明的工具名。
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def system_prompt_block(self) -> str:
        """返回注入 system prompt 的静态文本。空字符串表示跳过。"""
        return ""

    def shutdown(self) -> None:
        """清理退出 — 刷新队列、关闭连接。"""
