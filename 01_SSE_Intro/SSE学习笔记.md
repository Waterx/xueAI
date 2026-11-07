# SSE 入门：从 Demo 理解协议

## 1. SSE 解决什么问题

SSE（Server-Sent Events）让服务器通过一个持续打开的 HTTP 响应，向浏览器不断推送消息。浏览器先发起请求，之后服务器可以主动发送更新，无需浏览器每秒轮询。

SSE 是服务器到客户端的单向推送。客户端提交内容可以另发 HTTP 请求，例如 POST 提交问题，再通过 SSE 接收流式回答。若需要在同一连接上频繁双向通信，通常考虑 WebSocket。

| 场景 | 常见选择 |
| --- | --- |
| 实时日志、通知、任务进度 | SSE |
| 提交问题、流式接收回答 | HTTP 请求 + SSE |
| 频繁双向交互、协同编辑、实时游戏 | WebSocket |

## 2. 本项目如何运行

| 文件 | 作用 |
| --- | --- |
| `main.py` | FastAPI 后端，提供首页和 SSE 接口 |
| `index.html` | 原生 HTML、CSS、JavaScript 前端 |
| `requirements.txt` | Python 依赖 |
| `README.md` | 启动说明 |

Uvicorn 接收 HTTP 请求，FastAPI 根据路径调用对应函数。启动命令中的 `main:app` 表示 `main.py` 中的 `app` 对象，`--reload` 用于开发时自动重启。

执行流程：

```text
浏览器 GET / → 后端返回 index.html
点击“开始连接” → EventSource 发起 GET /events
后端保持响应打开 → 每秒发送一条 tick 事件
浏览器解析事件 → 页面与 Console 实时打印
```

首页加载和 SSE 连接是两个请求。前后端同源，所以本例无需配置 CORS。

## 3. /events 和 tick 的区别

```javascript
const source = new EventSource('/events');

source.addEventListener('tick', event => {
  console.log(event.data);
});
```

- `/events` 是 HTTP 接口路径，决定连接哪个地址。
- `tick` 是消息的事件名，决定由哪个监听器处理。
- 两者没有命名依赖，同一个连接可以发送 `tick`、`alert` 等多种事件。

先创建 EventSource，再立即注册监听器即可，无需等待连接成功。`addEventListener` 只在浏览器本地注册回调，不会向后端发送“订阅 tick”的请求。

后端省略 `event` 字段时，默认事件名为 `message`，可用 `source.onmessage` 或 `addEventListener('message', ...)` 接收。

## 4. 后端如何持续发送

```python
async def generate():
    while not await request.is_disconnected():
        yield "data: hello\n\n"
        await asyncio.sleep(1)

return StreamingResponse(generate(), media_type="text/event-stream")
```

`yield` 交出当前内容后还可以继续生成下一段，`StreamingResponse` 将这些内容分批发送。`await asyncio.sleep(1)` 在等待时让出执行机会，不会像 `time.sleep(1)` 那样阻塞事件循环。

`media_type="text/event-stream"` 声明响应采用 SSE 格式，但不会替你转换或校验内容。浏览器的 EventSource 才是按照 SSE 规则解析响应的客户端。

因此，`yield` 本身可以产生任意字符串，但要让 EventSource 正确接收消息，就必须输出符合 SSE 规则的文本。

## 5. SSE 格式速查

SSE 使用 UTF-8 文本，字段按行分隔，事件用空行结束。

```text
id: 1
event: tick
data: {"number": 1, "message": "你好"}

```

对应 Python：

```python
yield 'id: 1\nevent: tick\ndata: {"number": 1, "message": "你好"}\n\n'
```

| 字段 | 含义 |
| --- | --- |
| `data` | 消息内容；JSON 只是内容的一种选择 |
| `event` | 自定义事件名；省略时为 `message` |
| `id` | 消息 ID；自动重连时通过 Last-Event-ID 请求头携带 |
| `retry` | 自动重连间隔，使用纯数字毫秒数，例如 `retry: 2000` |
| `:` 开头的行 | 注释，不触发消息事件，可用于心跳 |

常见规则：

- 每行以第一个冒号分隔字段名和值，冒号后的一个可选空格会被移除。
- 字段名区分大小写，未知字段会被忽略；字段没有固定顺序。
- 换行支持 LF（`\n`）、CRLF（`\r\n`）和 CR（`\r`），通常用 `\n`。
- `\n\n` 中第一个换行结束字段，第二个形成空行，触发事件分发。
- 缺少结束空行的事件不会被分发，响应结束也不能代替空行。
- 只有 `event`、`id` 等字段而没有 `data` 时，不触发消息事件。
- 一次 `yield` 或一个网络传输块不一定对应一条事件；EventSource 根据文本内容解析边界。

最简单的消息：

```python
yield "data: hello\n\n"
```

多行内容要分别写 `data:`：

```python
yield "data: 第一行\ndata: 第二行\n\n"
```

前端得到的 `event.data` 为 `"第一行\n第二行"`。如果传递 JSON，使用 `JSON.parse(event.data)` 转为 JavaScript 对象。

以下写法不能作为 SSE 消息接收：

```python
yield '{"number": 1}'  # 缺少 data: 和结束空行
```

## 6. 断线重连与主动关闭

临时断线后，EventSource 通常自动重连。服务端可以发送 `retry: 2000` 设置重连间隔；致命错误可能使连接停止重试。

浏览器记住收到的 `id`，自动重连时通过 `Last-Event-ID` 请求头告诉后端。本例读取该值，从下一个编号继续生成消息。

**继续编号不等于补发历史消息。** 本例没有数据库或事件缓存，无法恢复断线期间错过的内容。

点击停止时：

```javascript
source.close();
source = null;
```

这会主动关闭请求并停止自动重连，不会额外调用一个后端“关闭接口”。再次创建 EventSource 是新连接，本例编号从 1 开始。

## 7. 后端如何处理连接关闭

Uvicorn 和 Starlette（FastAPI 的底层框架）感知客户端断开，StreamingResponse 随之结束。具体退出方式会受框架版本和运行环境影响，可能取消生成器正在执行的异步等待。

本项目也通过 `request.is_disconnected()` 主动检查断开，并在生成器的 `finally` 中记录退出：

```python
try:
    while not await request.is_disconnected():
        yield "data: hello\n\n"
        await asyncio.sleep(1)
finally:
    print("SSE 客户端已断开", flush=True)
```

生成器正常退出或被取消时，应在 `finally` 中释放它持有的订阅、上游连接等资源。前端断开不会自动终止你额外创建的独立后台任务，这类任务需要自己管理。

## 8. 如何观察与排查

- 页面和 Console：观察消息是否逐条到达。
- Network 中的 `/events`：连接长期处于 Pending 是正常现象。
- `curl -N http://127.0.0.1:8000/events`：查看原始文本，`-N` 关闭 curl 输出缓冲。
- 保持连接并重启后端：观察自动重连和编号继续。
- 经过反向代理时检查缓冲配置；本例发送 `X-Accel-Buffering: no` 提示 Nginx 禁用缓冲。

## 参考

- [WHATWG SSE 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html)
- [MDN：使用服务器发送事件](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
- [FastAPI：流式数据](https://fastapi.tiangolo.com/advanced/stream-data/)
