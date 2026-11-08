# 第 10 关：OpenAI ResponsesAdapter

进度：12/12。基于第 09 关独立复制，不重构 `BaseLLMAdapter`、`LLMEvent` 或 Registry。

| model | Adapter | 上游协议 |
| --- | --- | --- |
| `deepseek-flash`（默认） | AnthropicAdapter | Anthropic Messages |
| `deepseek-v4-pro` | ResponsesAdapter | OpenAI Responses |

## Checklist

- [x] 01 新建 `adapters/responses_adapter.py`，实现异步 `stream(messages, model)`。
- [x] 02 从 `DEEPSEEK_API_KEY` 初始化 AsyncOpenAI，base_url 为 `https://api.deepseek.com`，timeout=60.0、max_retries=0。
- [x] 03 使用 `await client.responses.create(..., stream=True)`，原样传入 model。
- [x] 04 system 内容按原顺序以空行合并为 instructions；user / assistant 转为 Responses 支持的字符串 message 列表，不修改原始 messages。
- [x] 05 `response.output_text.delta` 转为内部 delta，沿用 OutputMonitor。
- [x] 06 仅在 `response.completed` 且 status=completed、error 和 incomplete_details 为空时发送 done(stop)。
- [x] 07 最终 usage 的 input_tokens / output_tokens 映射为 prompt_tokens / completion_tokens，总量为两者之和。
- [x] 08 客户端与上游流均使用 async with；验证正常、异常、取消和主动关闭时释放资源。
- [x] 09 Registry 保留 Flash 路由，增加 Pro 路由。
- [x] 10 HTTP 和网页支持两个模型，默认仍为 Flash。
- [x] 11 使用真实 SDK + httpx.MockTransport 验证事件、消息转换、用量、异常、关闭与 Registry 路由。
- [x] 12 通过真实 HTTP `/chat` 分别验证 Flash 和 Pro。

## 行为

无 system 消息时沿用原有中文助手提示；HTTP 的消息角色限制与交替校验沿用 09，
system 转换能力在 Adapter 入口支持，不增加 HTTP system 权限。
Pro 设置 max_output_tokens=1024、reasoning.effort=none，延续原关卡的非思考调用。

成功 SSE：`start → delta × N → usage → done(reason=stop)`。
usage 缺失时不编造统计；零值保留。仅转发回答正文增量，不转发推理或重复的完整文本。
`response.failed`、`response.incomplete`、错误事件、完成状态异常和流提前结束均抛错，
由原有 `/chat` 转为 `error`，不生成成功 done。取消继续向上传播。

## 安装与运行（Windows PowerShell）

从工作区根目录安装依赖：

```powershell
.venv/Scripts/python.exe -m pip install -r 10_Responses/requirements.txt
cd 10_Responses
../.venv/Scripts/python.exe -m unittest discover -s tests -v
```

配置环境变量 DEEPSEEK_API_KEY 后启动：

```powershell
../.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8010
```

也可将密钥放在本目录 `.env`，启动命令追加 `--env-file .env`。
浏览器访问 http://127.0.0.1:8010，使用模型下拉框选择 Flash 或 Pro。

另开终端，在本关目录运行双模型真实验收（消耗少量 API 额度）：

```powershell
../.venv/Scripts/python.exe check_adapter.py --url http://127.0.0.1:8010
```

请求示例：

```json
{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"你好"}]}
```

原有 `{"prompt":"你好"}` 仍使用 Flash。未知模型在 HTTP 校验时返回 422。

## 验证记录（2026-11-03）

54 项 Mock 测试全部通过，未访问真实 API。
新增 `tests/test_responses_adapter.py` 包含 7 项测试，使用子测试覆盖多种异常和释放路径；
原有 47 项测试全部保留。

真实验收通过临时 uvicorn 服务的 `/chat`，使用 07 现有 `.env`，没有复制或输出密钥。

| model | delta 数 | prompt_tokens | completion_tokens | total_tokens | 最终事件 |
| --- | ---: | ---: | ---: | ---: | --- |
| deepseek-flash | 33 | 31 | 33 | 64 | done(stop) |
| deepseek-v4-pro | 34 | 30 | 34 | 64 | done(stop) |

两次调用均收到 start、连续 delta、usage、done；临时服务验收后关闭。
沙箱内首次联网因 APIConnectionError 失败，获准联网重试后通过。

## 官方参考

- [DeepSeek Responses 协议及兼容说明](https://api-docs.deepseek.com/guides/responses_api/)
- [OpenAI 官方流式 Responses 指南](https://developers.openai.com/api/docs/guides/streaming-responses)
