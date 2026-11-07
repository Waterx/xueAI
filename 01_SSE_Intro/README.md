# FastAPI SSE 实时打印 Demo

聊天中的协议要点与代码解读见 [SSE 学习笔记](SSE学习笔记.md)。

## 运行

```bash
cd 01_SSE_Intro
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

以上从项目根目录执行。若已在 `01_SSE_Intro` 内，可跳过 `cd`。
也可以复用根目录已有的虚拟环境：在本文件夹内执行 `../.venv/bin/python -m uvicorn main:app --reload`。

打开 http://127.0.0.1:8000，点击「开始连接」。页面与浏览器 Console 每秒打印一条消息。「停止连接」主动关闭连接。

也可以直接看原始响应（`-N` 关闭 curl 的输出缓冲）：

```bash
curl -N http://127.0.0.1:8000/events
```

## 理解协议

前端 `new EventSource('/events')` 发起 HTTP GET。后端返回 `Content-Type: text/event-stream`，保持响应打开，通过 `yield` 不断发送 UTF-8 文本：

```text
id: 1
event: tick
data: {"number": 1, "message": "服务器推送第 1 条消息"}

```

最后必须有空行（代码中的 `\n\n`），浏览器才把这一块作为完整事件分发。JSON 是本 demo 的内容选择，SSE 本身传的是文本。TCP/HTTP 传输块不一定与事件一一对应，EventSource 负责解析。

- `event: tick` 对应前端 `addEventListener('tick', ...)`；省略 event 时用 `onmessage`。
- `id` 会被浏览器记住，自动重连时通过 `Last-Event-ID` 请求头发送给后端。
- `retry: 2000` 表示断线后约 2 秒重试。主动 `.close()` 后不再重连。
- 以 `:` 开头的是注释，可用于心跳。本例每秒已有数据，因此只在连接时发送一次注释。
- SSE 是服务器到客户端的单向推送；客户端提交内容可另发普通 HTTP 请求。

本 demo 从上次 ID 的下一条继续生成编号，不保存、补发历史事件。点击停止后重新开始会创建新 EventSource，编号从 1 开始。前后端同源，无需额外配置 CORS。

在 Network 面板查看 `/events`，它长期处于 Pending 是正常的。尝试重启后端，观察自动重连。经过反向代理时也需要关闭该路径的缓冲；本例发送 `X-Accel-Buffering: no` 提示 Nginx 禁用缓冲。

参考：[MDN SSE](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)、[FastAPI StreamingResponse](https://fastapi.tiangolo.com/advanced/stream-data/)。
