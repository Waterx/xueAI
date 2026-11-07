# 05：请求级模型选择

基于 04_SSE_MultiTurn，保留多轮对话、停止生成和新对话功能。03、04 保持原样。

## 启动

在 xueAI 根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r 05_SSE_Model/requirements.txt
cd 05_SSE_Model
..\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8004
```

打开 http://127.0.0.1:8004 ，沿用系统环境变量 DEEPSEEK_API_KEY。

## 改造点

- ChatInput 使用 `model: Literal["deepseek-flash"] = "deepseek-flash"`，通过 Pydantic 限定允许值。
- 旧客户端不传 model 时使用默认值；显式传入不支持的名称、空字符串或非字符串会返回 HTTP 422，模型生成不会启动。
- main.py 把 body.model 传给 stream_reply(messages, model)，SSE start 同样使用 body.model。
- model.py 删除全局 MODEL 常量，上游 SDK 的 model 参数直接使用本次请求传入值。
- 前端增加只有 deepseek-flash 的模型选择框，并在 POST 请求中传递 model。

请求示例：

```json
{"model":"deepseek-flash","messages":[{"role":"user","content":"你好"}]}
```

省略 model 的旧请求仍有效：

```json
{"messages":[{"role":"user","content":"你好"}]}
```

同时兼容 03 的单轮协议 `{"prompt":"你好"}`，也支持附带 model。后端将 prompt 转换为一条 user message，再统一走多轮校验和模型调用。prompt 必须包含非空白文本且最多 2000 字符。没有有效输入会返回 422；非 null 的 prompt 与显式 messages 同时传入（即使 messages 为空）也返回 422，避免歧义。prompt 为 null 时视为未提供，可以单独使用 messages。

成功响应的首个 SSE 事件：

```text
event: start
data: {"model": "deepseek-flash"}
```

将 model 改为 gpt-unknown 会收到普通 HTTP 422 JSON 校验错误，而不会建立 SSE 流。

## 测试

在 05 项目目录执行（使用模拟模型流，不消耗 API 额度）：

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

对话历史仅存在页面内存中；刷新或新对话清空历史。只有完整成功的问答进入上下文。沿用 04 的 20 轮、消息长度限制及 1024 输出 token 上限。

## 捕捉异常控制标记

模型层检测 `<|im_end|>`，即使标记分散在多个 delta 中也会命中。检测后关闭本次模型流，通过 SSE error 返回诊断编号，不发送 done，因此本轮不进入历史。之前已经发送的部分文本仍保留显示。

在 `05_SSE_Model/logs/anomalies.log` 或后端终端搜索 `LLM_OUTPUT_ANOMALY`，可以找到时间、诊断编号、模型、上游 completion_id（若可用）、字符位置以及最近 16 个 delta（每个最多保留末尾 160 字符）。日志采用 UTF-8，每个文件上限约 2 MiB，保留 3 份轮转备份（anomalies.log.1 至 .3），首次异常时创建文件，路径不受启动目录影响。日志目录已加入 .gitignore。日志包含部分模型回答，不记录密钥和完整输入历史。这是 SDK 文本事件层的证据，不是原始 HTTP 响应抓包。

当前是针对已观察到的标记的精确检测，不是通用内容质量判断。模型正常讨论该标记时也会触发，不能仅凭命中判定上游服务故障。修改代码后，如未启用 --reload，需要重新启动 05 服务。
