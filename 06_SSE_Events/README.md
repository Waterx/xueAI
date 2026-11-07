# 06：有类型的内部事件

基于 05_SSE_Model，模型层与 HTTP 层通过 `LLMEvent` 通信。

- `schemas.py`：定义 `delta`、`done`、`usage` 三种事件，校验对应载荷。
- `model.py`：`stream_reply(messages, model) -> AsyncIterator[LLMEvent]`，不拼接 SSE。
- `main.py`：根据事件类型转换为 SSE，不再自行补发固定的 `done(stop)`。

内部事件与 SSE 的对应关系：

| 内部事件 | SSE event | SSE data |
| --- | --- | --- |
| `LLMEvent(type="delta", text="你好")` | `delta` | `{"text":"你好"}` |
| `LLMEvent(type="done", finish_reason="stop")` | `done` | `{"reason":"stop"}` |
| `LLMEvent(type="usage", usage={...})` | `usage` | `{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}` |

通过 `stream_options={"include_usage": True}` 请求用量，读取 SDK 最终汇总结果。
有用量时顺序为 `start → delta… → usage → done`；上游未返回用量时不伪造统计。
`done` 携带真实完成原因（例如 `stop`、`length`），保留 SSE 的 `reason` 字段以兼容原页面。
`done` 表示模型结束，`length` 表示达到输出上限；消费方可据此判断回答是否完整。
异常仍通过 `error` 事件返回，不补发 `done`。保留原有输出监测、取消和资源关闭逻辑。
页面沿用 05，不展示用量；可通过开发者工具或 HTTP 客户端查看 `usage` 事件。

## 启动

在工作区根目录执行，沿用环境变量 `DEEPSEEK_API_KEY`：

```powershell
cd 06_SSE_Events
..\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8005
```

打开 http://127.0.0.1:8005 。依赖与 05 相同，见 `requirements.txt`。

## 测试

在 06 目录执行；模拟上游 HTTP 流，不消耗 API 额度：

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

用量参数参考 [OpenAI 官方文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。实际用量是否返回取决于上游兼容接口。
