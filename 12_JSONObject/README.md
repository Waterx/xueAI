# 第 12 关：结构化输出（JSON Object）

基于第 11 关独立复制，进度 10/10。只实现非流式 JSON Object，不涉及 JSON Schema 或流式 JSON。

## Checklist

- [x] 01 ChatInput 增加 response_format，只接受 {"type":"json_object"} 或 None；拒绝额外字段。
- [x] 02 BaseLLMAdapter.complete() 和具体 Adapter 增加可选 response_format=None。
- [x] 03 Pro 的 Responses 请求增加 text={"format":{"type":"json_object"}}。
- [x] 04 Flash 使用 system Prompt 约束，保留原有 system 内容，要求只返回 JSON 对象且不包含 Markdown。
- [x] 05 complete_reply() 使用 json.loads() 本地验证，失败抛出 StructuredOutputError。
- [x] 06 返回 LLMResult，content 保持原始 JSON 字符串，其他字段不变。
- [x] 07 length 输出在解析前拒绝；Pro 的 incomplete 状态沿用原有异常处理。
- [x] 08 response_format 非空且 stream=true（包含省略 stream）返回 HTTP 422。
- [x] 09 两个 Adapter 参数、合法/非法 JSON、截断、普通文本及 SSE 回归测试通过。
- [x] 10 Flash 和 Pro 真实 JSON Object 请求通过。

## 运行

在本关目录执行：

```powershell
../.venv/Scripts/python.exe -m unittest discover -s tests -v
../.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8012 --env-file ../07_Adapter/.env
```

也可自行设置 DEEPSEEK_API_KEY。依赖见 requirements.txt。沿用的网页支持普通文本和 SSE；本关 JSON 模式通过 HTTP 请求使用。

## 请求示例

POST http://127.0.0.1:8012/chat

```json
{
  "model": "deepseek-flash",
  "stream": false,
  "response_format": {"type": "json_object"},
  "prompt": "请返回一个 JSON 对象，包含 name 和 lesson，lesson 为12。"
}
```

将 model 改为 deepseek-v4-pro 可验证 Pro。响应示例：

```json
{
  "model": "deepseek-flash",
  "content": "{\"name\":\"学习助手\",\"lesson\":12}",
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 61, "completion_tokens": 10, "total_tokens": 71}
}
```

外层仍是统一结果，content 是 JSON 字符串，调用方可再次 json.loads(result["content"])。

## 行为约定

不传 response_format（或显式传 null）时保留第 11 关行为：默认 stream=true，普通非流式文本不做 JSON 校验。
JSON 模式先检查 length，再解析正文；拒绝 Markdown 代码块、尾随文字、非法 JSON、NaN/Infinity 和非对象顶层值。
不自动修补、移除代码块或重试，避免把错误输出当作成功结果。

Flash 采用 Prompt 约束，并不保证上游必定遵循；本地验证确保只有通过校验的对象能作为成功响应。
Pro 同时使用原生 JSON Mode 与明确的 JSON 指令；普通文本请求不增加 text 参数。
HTTP 422 表示参数不支持；502 表示结构化输出校验失败或模型异常；504 表示 SDK 超时。
结构化校验错误提供明确原因，但不把失败正文放入 HTTP 错误。Pro 未完成状态继续返回通用模型失败错误。

## 验证记录（2026-11-03）

74 项自动化测试全部通过（继承 68 项 + 新增 6 项，新增测试内覆盖多个模型与边界子用例）。
使用真实 SDK + MockTransport，无需联网。首次从工作区根目录执行存在导入路径错误，切换本关目录后解决。
日志临时目录在沙箱中被拒绝，获准在沙箱外重跑后全部通过。

真实验收命令：

```powershell
../.venv/Scripts/python.exe check_json.py --env-file ../07_Adapter/.env
# 或针对运行中的服务：
../.venv/Scripts/python.exe check_json.py --url http://127.0.0.1:8012
```

默认通过 ASGITransport 进入本关 /chat 路由，上游为真实 API，不使用 Mock。
两个模型的 content 均通过 json.loads() 且解析为对象，finish_reason 均为 stop。

| 模型 | prompt_tokens | completion_tokens | total_tokens |
| --- | ---: | ---: | ---: |
| deepseek-flash | 61 | 10 | 71 |
| deepseek-v4-pro | 76 | 10 | 86 |

沙箱中首次联网失败，获准联网重试后通过。复用第 07 关现有环境文件，未复制或输出密钥。
check_adapter.py 保留原有两模型 × 流式/非流式文本验收，可使用 --url 指定服务。

## 官方参考

[DeepSeek Responses API：text.format 支持与 incomplete 状态](https://api-docs.deepseek.com/guides/responses_api/)
