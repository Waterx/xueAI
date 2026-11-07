# 第 14 关：流式结构化输出（Streaming JSON）

以 `13_SSE_JSONSchema` 为基线，支持 Flash / Pro 的 `json_object`、`json_schema` 流式及非流式输出。

## 启动

在本目录设置 `DEEPSEEK_API_KEY` 后运行：

```sh
python -m uvicorn main:app --reload --port 8012
```

## 验收

```sh
curl -N http://localhost:8012/chat -H 'Content-Type: application/json' -d '{"model":"deepseek-v4-pro","stream":true,"prompt":"提取信息：张三今年30岁。","response_format":{"type":"json_schema","name":"person","schema":{"type":"object","properties":{"name":{"type":"string"},"age":{"type":"integer"}},"required":["name","age"],"additionalProperties":false}}}'
```

将 model 改为 `deepseek-flash` 验证 Flash；将 response_format 改为 `{"type":"json_object"}` 验证 JSON Object；将 stream 改为 false 验证非流式。

流式成功顺序为 `start → delta × N → usage（如有）→ done`。模型层在 done 前拼接并校验完整 JSON；非法 JSON、Schema 不匹配、截断或上游异常均通过 error 结束，不发送 done。已经发送的 delta 无法撤回，客户端应等到 done 后才将完整 JSON 当作已验证数据。

Responses 通过 text.format 传入格式；Anthropic 使用 system 提示及本地校验，继续使用 text_stream。普通文本不缓存全文。

## 本地测试

```sh
python -m unittest discover -s tests -v
```

测试使用真实 SDK 和模拟 HTTP/SSE，覆盖四种流式组合、失败结束语义、取消/关闭及非流式回归；不代表真实远程 API 验收。
