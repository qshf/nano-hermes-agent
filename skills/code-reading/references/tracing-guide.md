# Tracing Guide — 深入追踪技巧

## grep 作为追踪工具

从调用点找定义：

```bash
# 找函数定义
grep -rn "def function_name" --include="*.py"

# 找所有调用点
grep -rn "function_name(" --include="*.py"

# 找类继承关系
grep -rn "class.*(.*ABC.*)" --include="*.py"
```

从数据找处理者：

```bash
# 谁在消费这个数据结构
grep -rn "class_name\." --include="*.py" | grep -v test

# 谁在修改这个变量
grep -rn "= .*variable_name" --include="*.py"
```

## 回溯调用栈

当深处调用链底层时，往上回溯：

1. 记下当前函数名
2. `grep -rn "函数名(" --include="*.py"` 找出所有调用者
3. 对每个调用者重复，直到回到入口
4. 连起来就是完整调用链

示例：

```
# 从 tool_result() 开始回溯
grep -rn "tool_result(" --include="*.py"
# → tools/skill_view_tool.py:108: return tool_result(output=content, ...)
# → tools/delegate_task.py:45: return tool_result(output=summary)

# 再往上：谁调了 skill_view_handler？
grep -rn "skill_view_handler" --include="*.py"
# → tools/skill_view_tool.py:139: registry.register(..., skill_view_handler, ...)
# → 这是注册为工具，调用者是 tool dispatch 机制
```

## ASCII 调用图

边读边画，纸笔也行，ASCII 也行：

```
main()
 └─ agent_loop()
     ├─ LLM.call()          ← 发 prompt，拿 response
     ├─ tool_dispatch()     ← 解析 tool_call，路由到 handler
     │   ├─ skill_view_handler()
     │   │   └─ SkillLoader.read_resource()
     │   └─ delegate_task()
     │       └─ child_loop()  ← 递归回 agent_loop
     └─ session_store.save() ← 持久化
```

画图的规则：竖线往下 = 调用了谁，箭头 = 数据流向，缩进 = 调用深度。

## 常见架构模式速查

| 模式 | 特征 | 阅读策略 |
|------|------|----------|
| **Strategy / ABC** | 抽象基类 + 多个具体实现 | 先读完 ABC 的接口文档，再选一个最简实现跟踪 |
| **Chain / Pipeline** | 数据穿过一串 handler（如中间件、transport chain） | 从第一个 handler 开始，逐个往下，每步看数据长什么样 |
| **Observer / Pub-Sub** | 事件触发 → 回调，grep "emit\|dispatch\|fire\|notify" | 先找事件定义（谁 emit），再找订阅者（谁 listen） |
| **Registry / Plugin** | 运行时注册 → 查找 → 调用，grep "register\|get_registered" | 从 register 点找所有注册者，从 dispatch 点看如何调度 |
| **Event Loop** | `while True` / `async for` / `asyncio.run` | 先理解一次循环（一个 iteration），再理解循环间状态 |
| **Repository / DAO** | CRUD 全走一层，grep "save\|load\|insert\|update" | 对着 schema（SQL / model）看操作，验证读写路径是否一致 |

## 多线程 / 异步的追踪陷阱

- **async/await**：追踪时不要跳过 `await`——控制权在 `await` 处交出，回来后的代码可能跑在不同状态上。每次 `await` 后都需要重新验证状态假设。
- **线程 / 进程**：grep `Thread`, `Process`, `ThreadPoolExecutor`。确认是否有共享状态，共享状态是 bug 高发区。
- **回调 / future**：找 `callback`, `add_done_callback`, `then`。回调的执行时机通常不是你写的顺序。

## 读 commit 历史获取意图

代码告诉你"做了什么"，commit message 告诉你"为什么"：

```bash
# 某个文件的改动历史
git log --oneline -10 -- path/to/file.py

# 某段代码是谁写的、为什么写的
git blame path/to/file.py | head -20

# 某次提交改了哪些文件
git show --stat <commit_hash>

# 看具体改动
git show <commit_hash> -- path/to/file.py
```

关键行 + `git blame` + `git show` 三连击，通常能在两分钟内搞清楚一段代码的动机。

## 当卡住时

| 症状 | 解法 |
|------|------|
| 不知道从哪开始 | 找一个你知道的入口点（CLI 命令、API 路由、main 函数），从那开始钻 |
| 钻得太深迷路了 | 回到上一个"你确定理解"的节点，重新往下走 |
| 看不懂某个函数 | 先看它的输入输出类型（类型标注、docstring），再看调用它的代码（它被怎么用） |
| 到处都是间接调用 | 找 concrete 实现（不是 ABC，不是 Protocol），从实现倒推抽象 |
