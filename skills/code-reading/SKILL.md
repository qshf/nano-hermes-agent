---
name: code-reading
description: "系统性阅读源码：定位入口、追踪数据流、识别模式、验证理解。不泛读，按调用链钻取。"
platforms: [linux, macos, windows]
---

# Code Reading

## 核心原则

不要泛读全文。从入口开始，沿着调用链一层层钻——每层只问三件事：

- **谁调了我？**（上游调用者）
- **我调了谁？**（下游依赖）
- **我改了什么数据？**（状态变化）

当你能不靠代码，自己画出整条链路，才算读懂了。

## 四阶段

### 阶段 1：定位入口

找到程序的"第一行"。不同项目类型有不同的入口特征：

| 项目类型 | 入口线索 |
|----------|----------|
| CLI 工具 | `def main()`, `if __name__ == "__main__"`, `click.command()`, `argparse` |
| Web 服务 | 路由注册 `app.get("/")`, `router`, 中间件链 |
| 库/框架 | `__init__.py` 公开的 `__all__`, 顶层 `class` 的 `__init__` |
| Agent/脚本 | `while True` 事件循环, `asyncio.run()`, prompt → LLM → tool 循环 |

**产出**：一张入口函数名 + 文件路径的清单。

### 阶段 2：追踪数据流

从入口出发，追踪数据（请求、消息、对象）如何流动。每一步记录：

```
入口 function_a()
  → 调 function_b(data)     # data 来自 CLI args
    → 调 function_c(transformed_data)   # 这里改了 data 的结构
      → 返回 Result 对象
        → function_a 把 Result 序列化为 JSON 输出
```

工具方法：`grep` 追踪函数定义、`git log` 看改动历史、IDE 跳转。

**产出**：一条或多条从入口到出口的完整调用链，标注每一步数据形态的变化。

### 阶段 3：识别模式

对照常见架构模式，判断代码属于哪种。不是为了套标签，而是为了**预测**——知道了模式，你就知道下一步该去哪看。

常见模式见 [references/tracing-guide.md](references/tracing-guide.md) 中的"常见架构模式速查"。

**产出**：一句话概括架构（如"Transport ABC → Chain 断路器 → Tool dispatch"）。

### 阶段 4：验证理解

用最小代价确认你的理解正确：

- 加一条 `print()` 或日志，跑一遍看输出是否如你所料
- 读对应的测试文件，测试即文档
- 向代码作者一句话描述你的理解，问"对吗？"

**产出**：确认正确的理解，或修正后的模型。

## 阅读技巧

| 技巧 | 说明 |
|------|------|
| 先看接口再看实现 | ABC / Protocol / 函数签名比实现更易读，先建立"能做什么" |
| 跟着错误走 | traceback 是最好的调用链说明书 |
| git blame 关键行 | 每行代码都有动机，commit message 告诉你为什么这么写 |
| 画出来 | ASCII 调用图、数据流箭头，画出来比盯着看快十倍 |
| 不纠结细节 | 递归深追会迷失。始终记得你当前在追的"主链路"是什么 |

更详细的追踪技巧见 [references/tracing-guide.md](references/tracing-guide.md)。

## 适用场景

- 新接手一个项目，需要快速建立心智模型
- 调试时看不懂报错上下文
- 给某段代码写测试前，需要先理解它
- 评估一个 PR 的影响范围


## 文档输出准则

阅读源码产出的所有文档（调用链笔记、架构分析、模式总结等），统一写入：

```
docs/值得讲的地方/new/
```

文件名用 `YYYY-MM-DD_<slug>.md` 格式，例如 `2025-06-05_skill-loader-call-chain.md`。

## 本 skill 的架构

这个 skill 本身是 v26.0 progressive disclosure 机制的一个示例——它在运行时经历三级披露：

1. **tier 1**：system prompt 中只注入 `code-reading: 系统性阅读源码...`（一行，几十 token）
2. **tier 2**：agent 调用 `skill_view("code-reading")` 拿到本文（四阶段 + 技巧表），返回中附 `linked_files` 告知还有 `references/tracing-guide.md`
3. **tier 3**：agent 调用 `skill_view("code-reading", "references/tracing-guide.md")` 拿到 grep 命令、模式速查表等深入技巧

详见 [docs/值得讲的地方/new/skill-system-v26.0-explained.md](../../docs/值得讲的地方/new/skill-system-v26.0-explained.md) 第九节。
