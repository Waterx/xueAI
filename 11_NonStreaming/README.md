# 第 11 关：非流式调用（Non-streaming）

基于第 10 关独立复制。同一个 `/chat` 支持 SSE 和普通 JSON，默认 `stream=true`，兼容旧客户端。
保留现有 Registry、两个模型和 SSE 处理，不增加 Provider。

第 11 关进度：10/10。

| model | Adapter | 上游协议 |
| --- | --- | --- |
| `deepseek-flash`（默认） | AnthropicAdapter | Anthropic Messages |
| `deepseek-v4-pro` | ResponsesAdapter | OpenAI Responses |

## Checklist

- [x] 01 `schemas.py` 定义 `LLMResult(model, content, finish_reason, usage)`。
- [x] 02 `BaseLLMAdapter` 保留 `stream()`，新增抽象异步 `complete()`。
- [x] 03 `ResponsesAdapter.complete()` 使用 `responses.create(stream=False)`。
- [x] 04 `AnthropicAdapter.complete()` 使用 `messages.create(stream=False)`。
- [x] 05 `complete_reply()` 通过 `get_adapter(model)` 动态路由，保留 `stream_reply()`。
- [x] 06 `ChatInput.stream: bool = True`。
- [x] 07 `/chat` 按 stream 返回 SSE 或普通 JSON。
- [x] 08 非流式失败返回 HTTP 502，SDK 超时返回 504；保留 SSE error 处理。
- [x] 09 两个 Adapter 的正文、usage、finish_reason、路由及异常 Mock 测试。
- [x] 10 Flash / Pro × stream=true / false 真实调用验收。

## 运行

从本关目录执行（依赖沿用第 10 关，测试另需 httpx）：

```powershell
../.venv/Scripts/python.exe -m pip install -r requirements.txt
../.venv/Scripts/python.exe -m unittest discover -s tests -v
../.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8011
```

启动前设置 `DEEPSEEK_API_KEY`，也可通过 uvicorn 的 `--env-file .env` 加载。
网页可通过“调用方式”下拉框选择流式或非流式，两个模型都支持。右侧日志显示实际请求的 stream 值以及 SSE 事件或完整 JSON 响应。
非流式接口也可按下列方式调用。

## 请求与结果

流式（不传 stream 时行为相同）：

```json
{"model":"deepseek-flash","prompt":"你好","stream":true}
```

非流式（更换 model 可调用 Flash）：

```json
{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"你好"}],"stream":false}
```

JSON 结果示例，用量以实际调用为准：

```json
{
  "model": "deepseek-v4-pro",
  "content": "你好！有什么可以帮助你？",
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 8, "completion_tokens": 10, "total_tokens": 18}
}
```

PowerShell：

```powershell
$body = @{ model = 'deepseek-flash'; prompt = '你好'; stream = $false } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8011/chat -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

## 结果与错误约定

- 两种模式使用同一组模型、参数和默认中文提示。
- Pro 提取 `output_text`，仅接受 `status=completed` 且 error、incomplete_details 为空；其他状态抛错，沿用第 10 关规则。
- Flash 按顺序拼接所有 text 块，忽略 thinking 等非正文块；`end_turn/stop_sequence → stop`，`max_tokens → length`，其他 stop_reason 抛错。
- usage 统一映射为 `prompt_tokens/completion_tokens/total_tokens`，总量为输入与输出之和；缺失时为 `null`，零值保留。
- 完整正文也经过 OutputMonitor，异常控制标记不会作为成功 JSON 返回。
- HTTP 422 表示请求校验失败；502 表示模型调用或完成状态异常；504 表示 SDK 调用超时。错误格式为 `{"detail":"模型生成失败，请重试。"}` 或对应超时文案，不暴露上游异常细节。
- SSE 继续使用 `start → delta × N → usage → done`，流中失败发送 error；取消向上传播，两种模式都会关闭客户端资源。
- `adapters/openai_adapter.py` 是保留的历史流式兼容实现，未注册到当前路由。其 `complete()` 明确抛出 NotImplementedError；本关只扩展实际使用的两个 Adapter。

## 验证记录（2026-11-03）

68 项 Mock 测试全部通过（原有 54 项 + 新增 14 项），使用真实 SDK + httpx.MockTransport，不访问外网。
覆盖多文本块、system/history 转换、缺失与零用量、完成状态、动态 Registry、未知模型、
上游错误、连接失败、超时、取消、客户端释放、输出异常和旧 prompt 请求。
既有 SSE 回归测试保持通过。

真实四组合验收通过运行中的 HTTP `/chat` 执行：

```powershell
../.venv/Scripts/python.exe check_adapter.py --url http://127.0.0.1:8011
```

此命令依次调用 Flash 流式、Flash 非流式、Pro 流式、Pro 非流式，会消耗少量 API 额度。
四种组合全部通过，均以 `stop` 结束：

| 模型 | stream | 结果 | prompt_tokens | completion_tokens | total_tokens |
| --- | --- | --- | ---: | ---: | ---: |
| deepseek-flash | true | start、35 个 delta、usage、done | 31 | 35 | 66 |
| deepseek-flash | false | 普通 JSON，正文非空 | 31 | 29 | 60 |
| deepseek-v4-pro | true | start、37 个 delta、usage、done | 30 | 37 | 67 |
| deepseek-v4-pro | false | 普通 JSON，正文非空 | 30 | 32 | 62 |

真实验收使用 07 现有 `.env`，未复制或输出密钥；临时 uvicorn 服务在验收后关闭。
沙箱内首次联网返回 APIConnectionError，获准联网重试后通过。
日志测试在沙箱内因临时目录权限失败，获准在沙箱外执行全量测试后 68 项全部通过。

## 官方参考

- [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)
- [Anthropic Messages 与 stop_reason](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
