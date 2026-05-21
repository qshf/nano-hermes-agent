"""
MemoryProvider ABC — 记忆后端的统一契约。

V7 核心：定义任何记忆后端必须满足的接口。
V9 扩展：新增生命周期方法，让 manager 能在 agent loop 的正确时机广播事件。
V13 扩展：新增 queue_prefetch()，实现两阶段预热模式。

核心方法（必须实现）：
- name: 短标识符
- is_available(): 是否就绪
- initialize(): 每会话初始化
- get_tool_schemas(): 暴露给模型的工具 schema
- handle_tool_call(): 分发工具调用
- system_prompt_block(): 注入 system prompt 的静态文本
- shutdown(): 清理退出

V9 生命周期钩子（默认 no-op，按需 override）：
- on_turn_start(): 每轮开始时通知（轮数计数、定期维护）
- prefetch(): 每轮前根据用户查询召回相关上下文
- sync_turn(): 每轮结束后持久化完成的对话
- queue_prefetch(): 每轮结束后启动后台预热，供下一轮 prefetch() 消费（V13）

为什么 prefetch/sync_turn 是默认实现而不是 abstractmethod：
内置 provider（文件存储）通过 system_prompt_block 一次性注入全部记忆，
不需要召回逻辑；只有外部 provider（语义搜索/知识图谱）才需要 override。
强制所有子类实现会污染最简实现。
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
        """返回注入 system prompt 的静态文本。空字符串表示跳过。

        用于 STATIC provider 信息（指令、状态）。动态召回内容应该
        通过 prefetch() 注入 user message，避免破坏前缀缓存。
        """
        return ""

    def shutdown(self) -> None:
        """清理退出 — 刷新队列、关闭连接。"""

    # ─── V9 生命周期钩子（默认 no-op）─────────────────────────────────────

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """每轮开始时调用，传入用户消息。

        用于轮数计数、作用域管理、定期维护。
        kwargs 可能包含 model、platform 等，provider 按需取用，多余的忽略。
        """

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """为即将到来的一轮召回相关上下文。

        在每次 API 调用前被调用。返回格式化文本注入 user message，
        或空字符串表示无相关内容。

        V13 两阶段模式：如果上一轮 queue_prefetch 已预热，这里只需
        消费缓存结果（近零延迟）；冷启动时 fallback 到同步 HTTP 调用。
        """
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """持久化完成的一轮对话到后端。

        在每轮 tool loop 结束、最终文本响应到达后被调用。
        V12 起非阻塞 — 通过后台 writer 线程入队。
        """

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """在当轮结束后启动后台预热，供下一轮 prefetch() 消费。

        在 sync_turn 之后调用。实现应启动后台线程执行 recall/reflect，
        将结果缓存到实例变量，下一轮 prefetch() 直接取用。
        默认 no-op — 只有需要后台预热的 provider 才 override。
        """

