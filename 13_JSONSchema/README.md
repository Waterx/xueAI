# 第 13 关：JSON Schema 结构化输出

从第 12 关独立复制，沿用 HTTP → model → Registry → Adapter 架构。
仅非流式支持结构化输出，不引入 Service、Tool Use、自动修复或重试。

## Checklist：10/10

- [x] 01 安装 jsonschema，更新 requirements.txt。
- [x] 02 ResponseFormat 支持 json_object / json_schema，后者必填 name 和 schema。
- [x] 03 BaseLLMAdapter 和 Adapter 的参数改为 dict[str, Any]。
- [x] 04 使用 Draft202012Validator.check_schema()，非法 Schema 在调用模型前返回 422。
- [x] 05 Pro 使用 Responses API 原生 text.format，同时保留 json_object。
- [x] 06 Flash 将序列化 Schema 加入 system instruction，保留原有 system 内容。
- [x] 07 json.loads() 后使用 jsonschema.validate() 检查 Schema。
- [x] 08 非法 JSON 和约束违规统一抛出 StructuredOutputError，HTTP 返回 502。
- [x] 09 json_object、普通文本、SSE 回归；结构化输出与 stream=true 组合返回 422。
- [x] 10 Flash / Pro 成功及校验失败测试，真实模型验收另见验证记录。

## 安装与运行

在本关目录执行（macOS，复用工作区虚拟环境）：

```bash
../.venv-mac/bin/python -m pip install -r requirements.txt
../.venv-mac/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8012 --env-file ../07_Adapter/.env
```

也可自行设置 DEEPSEEK_API_KEY。网页沿用普通文本 / SSE；结构化输出通过 HTTP 使用。
name 为 1–64 个字母、数字、下划线或连字符；response_format 拒绝未知字段。
schema 通过别名保存为 json_schema，避免覆盖 Pydantic 的 schema() 方法。

## 验收请求

```bash
curl -X POST http://localhost:8012/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-pro",
    "stream": false,
    "prompt": "提取信息：张三今年30岁。",
    "response_format": {
      "type": "json_schema",
      "name": "person",
      "schema": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "age": {"type": "integer"}
        },
        "required": ["name", "age"],
        "additionalProperties": false
      }
    }
  }'
```

将 model 改为 deepseek-flash 即可验证 Flash。返回 LLMResult，content 保留原始 JSON 字符串，finish_reason 为 stop。

## 验证规则

JSON Schema 在 JSON 语法之上约束字段和内容。例如 age 必须为 integer，字符串“三十”会失败；required 限制必填字段，additionalProperties=false 禁止额外字段。

- 客户端 Schema 不合法或缺少必填参数：422，模型不会被调用。
- 输出不是合法 JSON、缺字段、类型错误或额外字段违规：502，错误正文不包含模型输出。
- 截断输出：502；SDK 超时：504。
- json_object 要求顶层对象；json_schema 的顶层类型由 Schema 决定，也接受合法的布尔 Schema。
- Schema 自检与结果校验统一使用 Draft 2020-12；不额外启用 format 断言。
- 保留 NaN / Infinity 拒绝逻辑，不修复、不重试。

Flash 的 Prompt 约束不能保证输出遵守 Schema，因此两个协议都在本地强制校验。
Pro 的 incomplete / failed 状态继续由 Adapter 拒绝。

## 验证记录（2026-11-05）

```bash
../.venv-mac/bin/python -m unittest discover -s tests -q
../.venv-mac/bin/python check_schema.py --env-file ../07_Adapter/.env --regression
# 或对已运行服务验收：
../.venv-mac/bin/python check_schema.py --url http://127.0.0.1:8012 --regression
```

78 项自动化测试通过（继承 74 项，新增 4 项，每项覆盖多个子用例）。
新增测试经 HTTP 路由、真实 SDK 和 MockTransport 验证两模型的成功与失败，包括缺字段、错误类型、额外字段、非法 JSON、非法 Schema、数组和布尔 Schema、流式限制。
失败输出使用 MockTransport 注入，确保可重复验证 HTTP 502；不宣称真实模型必定生成违规内容。

真实模型验收使用现有环境文件，仅加载密钥，不复制或输出；经本关 ASGI 路由调用真实上游。

## 官方参考

- [DeepSeek Responses API：text.format 支持](https://api-docs.deepseek.com/guides/responses_api/)
- [DeepSeek Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)

真实验收结果：Flash 和 Pro 均返回 `{"name":"张三","age":30}`，finish_reason 为 stop；两者的 json_object、普通非流式文本与 SSE 全部通过。

| 模型 | prompt_tokens | completion_tokens | total_tokens |
| --- | ---: | ---: | ---: |
| deepseek-flash | 98 | 9 | 107 |
| deepseek-v4-pro | 102 | 9 | 111 |
