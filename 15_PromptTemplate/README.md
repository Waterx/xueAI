# 第 15 关：Prompt 模板与变量替换

基于 `14_StreamingJSON`，新增内存模板管理。模板渲染在模型调用前完成，Flash / Pro 共用同一条解析路径，Adapter 保持原样。

## 启动

在本目录设置 `DEEPSEEK_API_KEY` 后运行：

```sh
python -m uvicorn main:app --reload --port 8012
```

## 请求方式

`prompt`、`template_id`、原有多轮 `messages` 三种入口只能选一种。直接 prompt 保留非空白、最多 2000 字符的校验；模板渲染后的最终 prompt 同样受此限制。`stream` 默认 true。

```json
{
  "model": "deepseek-flash",
  "template_id": "translate",
  "variables": {"text": "Good morning", "language": "中文"},
  "stream": true
}
```

固定模板位于 `prompt_manager.py`：

| ID | 模板 | 必需变量 |
| --- | --- | --- |
| translate | `请把 {{text}} 翻译成{{language}}` | text、language |
| summarize | `请总结以下内容：` + 换行 + `{{text}}` | text |

`render_prompt(template_id, variables)` 只查找、校验和替换，不调用模型。模板不存在、缺少变量、变量值非字符串时返回 HTTP 422；额外变量忽略。变量值按普通文本插入，不执行表达式、不解释反斜杠、不递归替换。空字符串可以作为变量值。

例如 text 为 `{{language}}`、language 为 `日语`，结果为 `请把 {{language}} 翻译成日语`。

模板请求继续支持 `response_format`，例如：

```json
{
  "model": "deepseek-v4-pro",
  "template_id": "summarize",
  "variables": {"text": "提取姓名和年龄：张三今年30岁。"},
  "stream": false,
  "response_format": {
    "type": "json_schema",
    "name": "person",
    "schema": {
      "type": "object",
      "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
      "required": ["name", "age"],
      "additionalProperties": false
    }
  }
}
```

流式成功事件保持 `start → delta × N → usage（如有）→ done`。完整 JSON 在 done 前进行校验；失败发送 error，不发送 done。模板错误在开始流之前返回 422。

## 本地验收

```sh
python -m unittest discover -s tests -v
```

共 87 项测试，包含继承的第 14 关回归与新增模板验收 T1–T9。新增矩阵验证两个模型 × 流式/非流式 × 普通文本/JSON Object/JSON Schema × prompt/模板，共 24 种组合，并检查渲染后的消息确实进入真实 SDK 请求体。

测试使用真实 SDK 与模拟 HTTP/SSE，不访问远程模型，不代表真实 API 验收。模板仅存于内存，不包含数据库、版本、CRUD 或高级模板语法。
