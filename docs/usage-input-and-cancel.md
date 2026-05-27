# 使用指南 — 输入与中断（V22）

> 适用版本：v22 起。前 21 档用 Python 内建 `input()`，本指南内容不适用。

V22 把命令行输入换到 `prompt_toolkit` 的 `PromptSession`，并在流式响应期间
接管 `SIGINT` 把"杀进程"翻译成"取消当前响应回到 prompt"。下面是日常用得到
的所有按键组合。

---

## 1. 输入框（`You > ` 提示符上）

| 按键 | 行为 |
|------|------|
| 普通字符 | 插入光标位置；CJK / Emoji / IME 候选窗口都正常 |
| `Backspace` | 按字符删除（不再有 macOS libedit 的 byte-vs-char bug） |
| `Enter` | 提交当前输入给 agent |
| **`Esc → Enter`** | **在当前位置插入换行**（多行输入；提交仍用普通 `Enter`） |
| `↑` / `↓` | 翻历史命令（持久化在 `~/.nano_hermes_history`） |
| `Ctrl+A` / `Ctrl+E` | 跳到行首 / 行尾 |
| `Ctrl+W` | 删除光标前一个词 |
| `Ctrl+U` | 删除光标到行首 |
| `Ctrl+K` | 删除光标到行尾 |
| `Ctrl+L` | 清屏（保留当前输入） |
| `Ctrl+C` | 退出 agent（与 `quit` / `exit` / `q` 等价） |
| `Ctrl+D` | 退出 agent（EOF） |

### 关于 Esc-Enter 多行的几个细节

**正确的按法**：先按 `Esc`（松开），再按 `Enter`。**不是同时按住**。
prompt_toolkit 把它识别成 ESC 序列 + `\r`，绑定回调在缓冲里插入一个 `\n`。

**macOS Terminal.app 不识别 Option-Enter？** 这是 Terminal 的默认行为 —
Option 键不发 Esc 序列。两个解法任选其一：

1. **改 Terminal 设置**：Settings → Profiles → Keyboard → 勾 "Use Option
   as Meta key"。之后 Option-Enter ≈ Esc-Enter。
2. **换 iTerm2 / Alacritty / WezTerm**：默认就把 Option-Enter 发成 ESC+CR。

**多行示例**：
```
You > 帮我看下这段代码：[Esc][Enter]
def foo():[Esc][Enter]
    return 42[Enter]
```
最后一个 `Enter` 才提交，前面两次 `Esc-Enter` 只是换行。

---

## 2. 流式响应中（agent 正在打印 token）

`/stream on`（默认）下，模型 token 一个个流出来。这时按键的语义和 prompt
上完全不同 —— V22 启动了一个后台 stdin 监听器接管单键输入，主信号
handler 同时接管 `SIGINT`。

| 按键 | 行为 |
|------|------|
| **`Esc`** | **当帧停止流，打印 `[cancelled]`，回到 `You > ` 提示符**（推荐用这个） |
| `Ctrl+C` | 同 Esc — 通过 `SIGINT` handler 走同一条 cancel 路径（备用通路） |
| 其他键 | 流式期间不响应，被 cbreak 监听器忽略 |

### 为什么默认推 Esc 而不是 Ctrl+C

`Ctrl+C` 在 prompt 上是"退出 agent"，在流式中是"取消响应" —— 同一组键两种
语义，按错了就退出整个进程。`Esc` 在两种状态下语义统一：**取消当前操作**
（prompt 上 Esc 不做事；流式中 Esc 取消响应），更不容易误操作。

### 中断后的状态

- 当前轮的 assistant 消息**不会**写进对话历史 —— 你看到的半截 token 不会
  污染下一轮 messages
- 已经执行过的 tool（如本轮先调了 `read_file` 再被打断）保留在历史里
- `cancel_token` 自动 reset，下一次输入照常走流式
- 终端 termios 状态会被还原，prompt 输入正常

### 实现细节（如果想知道）

- 流式开始：`_stream_one_turn` 启动后台线程 `_esc_listener`，把 stdin 切到
  cbreak 模式，循环读单字节，见到 `\x1b` (Esc) 就 `cancel_token.cancel()`
- 流式结束：主线程 `stop_event.set()`，listener 退出，**termios 恢复**
- 非 tty 环境（pytest / 管道）：`termios.tcgetattr` 抛异常，listener 静默退出，
  Ctrl+C 仍可用（走 SIGINT）
- 与源项目 hermes-agent [`cli.py:11372`](file:///Users/qshf/my-project/hermes-agent/cli.py#L11372)
  的差异：源项目把 agent 跑后台线程、prompt_toolkit 持续活跃用 KeyBinding
  捕获 Ctrl+C；nano 主循环单线程，用临时 cbreak 监听器更轻

---

## 3. 切换流式 / 非流式

```
/stream            # 显示当前状态
/stream on         # 启用流式（默认）
/stream off        # 禁用流式 — 整段响应到达后一次性打印
```

非流式（`/stream off`）下：
- 模型整段写完才显示，等待期间黑屏 5–30s
- Ctrl+C 会**杀进程**（同步 `chain.call` 不响应 cancel token）—— 这是
  V21 之前的行为，V22 保留是为了对比 / 调试
- usage / cache 命中率与流式完全等价（V22 验证条目之一）

正常使用建议保持 `/stream on`。

---

## 4. 启动期 env 开关

```bash
# 启动时直接关流式（仍可在运行期 /stream on 打开）
STREAM_ENABLED=0 python main.py

# 默认开
python main.py
```

---

## 5. 常见问题

**Q：粘贴多行代码后 Enter 立即提交了，没机会编辑**
A：粘贴前先按一次 `Esc → Enter` 进入多行视觉，再粘贴；或者粘贴后用
`↑` 回到行首逐行检查（PromptSession 把粘贴块当成单 buffer，光标可上移）。

**Q：历史命令窜上来一堆 slash 命令，找正常对话麻烦**
A：直接用 `Ctrl+R` 反向搜索。输入关键词 prompt_toolkit 会从历史里筛。

**Q：流式期间按 Esc / Ctrl+C 没反应，过了 5 秒才停**
A：少数 provider 的 SDK 在 socket 层做了 buffered read，`cancel_token.check()`
要等下一帧到达才生效。非常长的 reasoning chunk 之间也会有几秒间隔。这是
SDK 的限制，不是 nano 的 bug。

**Q：中断后下一次 prompt 上的输入显示乱码或不响应**
A：流式打印途中被切断时，部分 ANSI 颜色序列可能没被关闭；正常情况下
``_esc_listener`` 的 finally 块会还原 termios，但如果发生了 hard kill
（如 SIGKILL）状态可能残留。在 prompt 上按一次 ``Ctrl+L`` 清屏，或运行
``stty sane`` 还原终端属性。

**Q：在 SSH 上 Esc 不工作 / 触发延迟**
A：远端 terminal emulator 决定 Esc 序列。tmux 用户记得 ``set -g escape-time 0``
否则 tmux 会吞 Esc 之后的 50ms。screen 用户类似。Esc-Enter 多行同理。

---

## 6. 与源项目 hermes-agent 的对应

源项目 [`cli.py`](file:///Users/qshf/my-project/hermes-agent/cli.py) 用的也是
`prompt_toolkit`，但绑定更细（vi-mode、bracketed paste 拒绝、autopilot 联动
等），nano 只保留 V22 教学需要的最小集：FileHistory + Esc-Enter + 默认 emacs
模式 + SIGINT 流式取消。如果想要 vi 编辑模式 / 自动补全 / 多 buffer，参考源
项目 cli.py 的 `_make_session` / `_make_key_bindings`。
