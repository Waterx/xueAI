# 第 20 关 · 按模型独立限流

Flash（Anthropic Messages）和 Pro（OpenAI Responses）共用 `POST /chat` 的错误契约。请求可通过第 17 关的 SQLite 指标定位。


## 本关实现

- `rate_limiter.py` 使用每模型独立 deque、单调时钟与 `asyncio.Lock` 实现 60 秒滑动窗口。默认 Flash 5 次、Pro 3 次，可修改 `MODEL_LIMITS` 配置。
- 清理、检查和登记在同一短临界区执行；模型调用不持锁。恰好满 60 秒的旧记录即过期。
- 校验与模板解析成功后检查一次额度，非流式和 SSE 共用入口。超限直接返回 HTTP 429 JSON，不发送 SSE start，也不调用 Provider。
- 本地错误为 `RATE_LIMIT_EXCEEDED`、`retryable=false`，含请求 ID；`Retry-After` 是最早记录过期前剩余秒数向上取整。
- 放行后即消耗一次额度，包括最终失败或取消的请求；拒绝和校验失败不消耗额度。第 19 关内部重试不重复扣额度。
- 上游 429 仍映射为 HTTP 503 / `UPSTREAM_RATE_LIMITED` 并按原规则重试。
- 本地拒绝不写入模型调用指标；放行请求保留原有的一次请求一条指标，以及 SSE、模板、结构化输出、取消和重试行为。
- 仅支持单进程、单事件循环内存配额，重启会清空。运行一个 Uvicorn worker；多个 worker/实例不会共享配额。本关不增加 Redis、用户/IP 限流或排队。

### 超限响应

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 13
X-Request-ID: <request-id>
```

```json
{"error":{"code":"RATE_LIMIT_EXCEEDED","message":"当前模型请求过于频繁，请稍后重试。","retryable":false,"request_id":"<request-id>"}}
```

`Retry-After` 是建议等待时间，并不预留下一次额度。客户端可等待后重新发起请求；`retryable=false` 表示不进入网关的上游自动重试流程。

## 启动

在本目录设置 `DEEPSEEK_API_KEY`，运行：

```powershell
..\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8013
```

macOS 可使用 `../.venv-mac/bin/python -m uvicorn main:app --reload --port 8013`。

首次访问模板时自动创建本目录的 `prompts.db`，路径由 `Path(__file__)` 确定，与启动工作目录无关。默认 `translate`、`summarize` 通过 `INSERT OR IGNORE` 写入版本 1，重复初始化不会增加版本或覆盖内容。数据库及日志已加入本目录的忽略规则。

## 错误契约

非流式请求失败时返回表中的 HTTP 状态码，JSON 结构如下。

```json
{
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "模型调用超时，请重试。",
    "retryable": true,
    "request_id": "e4f8b18d-2c9b-4c5d-a1f3-5a6d1b1a7f20"
  }
}
```

SSE 已开始时，HTTP 状态保持 200，通过 `event: error` 发送错误对象。

```text
event: error
data: {"code":"UPSTREAM_TIMEOUT","message":"模型调用超时，请重试。","retryable":true,"request_id":"e4f8b18d-2c9b-4c5d-a1f3-5a6d1b1a7f20"}
```

| 错误码 | HTTP | retryable |
| --- | --- | --- |
| `RATE_LIMIT_EXCEEDED` | 429 | false |
| `INVALID_REQUEST` | 422 | false |
| `UPSTREAM_AUTH_ERROR` | 502 | false |
| `UPSTREAM_RATE_LIMITED` | 503 | true |
| `UPSTREAM_TIMEOUT` | 504 | true |
| `UPSTREAM_UNAVAILABLE` | 503 | true |
| `STRUCTURED_OUTPUT_INVALID` | 502 | false |
| `UPSTREAM_BAD_RESPONSE` | 502 | false |
| `INTERNAL_ERROR` | 500 | false |

上游 429 映射为 `UPSTREAM_RATE_LIMITED`。`retryable` 表示故障类型具备重试条件。客户端收到部分 SSE 内容后，应将错误视为本轮失败。

请求体或模板校验失败时，两种模式都返回 HTTP 422 和 `INVALID_REQUEST`，且没有模型调用指标。模型调用失败记录为 `error`，客户端中断记录为 `cancelled`。错误对象、响应头 `X-Request-ID` 与模型调用记录使用同一个请求 ID。

## 查询模型指标

```bash
curl http://localhost:8013/metrics
```

返回按模型分组的已结束调用。首次调用结束前返回 `{}`。每组包含请求总数、成功数、错误数、取消数、Token 累计值，以及 `avg_latency_ms` 和 `avg_ttft_ms`。

- 总延迟从模型调用开始计时，包含失败和取消。请求校验及模板读取在计时范围之外。
- TTFT 取首个非空文本增量，只对实际测得 TTFT 的调用求平均。没有样本时返回 `null`。
- Token 汇总只累加已经收到的 Provider Usage，缺失字段不参与求和。汇总显示 `0` 可能是上报值之和为零，也可能是没有收到该字段。它无法证明未上报用量的请求没有消耗 Token。
- SQLite 保存模型层收到的完整统一 Usage。流式调用在迭代结束后入账，也保留 `done` 之后到达的 `usage`。输入、输出和总 Token 继续使用独立列汇总。
- 每次已结束的模型调用写入本目录的 `metrics.db`。汇总基于这些记录，服务重启后仍可查询。

`GET /metrics/requests?limit=50` 返回最新的单次调用记录，包含 ID、UTC 入库时间、模型、流式标记、结果、总延迟、TTFT 和 Token 用量。`usage` 返回模型层提供的全部字段，缺失时为 `null`。旧记录继续保留已有的 Token 列，完整 `usage` 为 `null`。缺失的 TTFT 或 Token 字段也为 `null`。使用 `before_id` 传入上一页最后一条记录的 ID，即可查询更早的数据。记录只包含观测字段。

每次 `POST /chat` 都会收到服务端生成的 `X-Request-ID`。通过请求校验后，响应还会返回 `X-Conversation-ID`。后续轮次在请求体中传入该 UUID 作为 `conversation_id`；省略时分配新会话 ID。两个 ID 会随调用记录入库并写入完成日志。

`GET /metrics` 和 `GET /metrics/requests` 均支持 `conversation_id` 和 `request_id` 查询参数，可以组合使用。聊天页可以在新标签页打开独立观测页 `/observability`，查看汇总、请求明细和筛选结果。旧记录的两个 ID 为 `null`，仍可在未筛选的列表中查看。

## 创建模板

`POST /templates`：

```json
{"template_id":"greeting","content":"请用{{language}}向{{name}}问好"}
```

返回：

```json
{"template_id":"greeting","version":1}
```

每次保存都创建一个新版本，包括相同内容；各模板独立从 1 递增。`BEGIN IMMEDIATE` 将查询最大版本和插入放在同一事务中，复合主键防止重复版本。ID 和内容必须是非空白字符串；非法输入返回 422。此接口不接受客户端指定版本。

## 使用模板

`POST /chat`：

```json
{
  "model":"deepseek-flash",
  "template_id":"greeting",
  "template_version":1,
  "variables":{"language":"日语","name":"张三"},
  "stream":false
}
```

省略 `template_version`（或传 null）使用最新版；指定时必须是大于等于 1 的整数，不接受布尔值、小数或字符串。只有提供 `template_id` 时才能设置版本。

`prompt`、`messages`、`template_id` 仍然三选一。不存在的模板或版本、缺少变量、渲染结果为空或超过 2000 字符均在调用模型前返回 422。额外变量被忽略，变量值保持字面含义，不递归替换。流式及 `response_format` 用法与第 15 关一致。

## Python 接口

- `init_db()`：幂等创建表和默认模板。
- `save_template(template_id, content) -> int`：保存并返回新版本号。
- `get_template(template_id, version=None) -> str`：获取最新或历史内容，不存在抛出 ValueError。
- `render_prompt(template_id, variables, version=None) -> str`：查询后复用原有变量校验和单次替换。

## 验证

在本目录运行：

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS 可使用 `../.venv-mac/bin/python -m unittest discover -s tests -v`。测试通过模拟上游验证两种协议的错误分类、HTTP 与 SSE 的请求 ID、SQLite 失败记录和取消行为，并覆盖模板、普通回答与结构化输出的成功路径。

保留上一关的普通 prompt、多轮 messages、两个模型、SSE、JSON Object 和 JSON Schema 回归。测试使用隔离临时数据库及模拟 HTTP，不访问远程模型服务。

指标测试另覆盖双模型统计、首个非空增量、延迟到达的用量、结构化输出错误和 SSE 断开。

### 第 20 关验收

`tests/test_rate_limit.py` 覆盖 T1–T12：双模型独立配额、默认限额、滑动窗口精确到期、并发争抢最后额度、HTTP/SSE 429 格式与请求 ID、动态 Retry-After 向上取整、上游 429、双模式内部重试只扣一次额度、无效参数与模板不扣额度、本地拒绝不产生指标。另验证失败占用额度、拒绝不延长窗口和配置校验。

旧功能回归使用每测试独立的高额度限流器，避免跨用例累积配额；限流专项测试使用真实的低额度限流器、可控单调时钟及模拟 Provider，无须实际等待 60 秒或访问远程模型。

2026-11-08：运行 `../.venv-mac/bin/python -m unittest discover -s tests -v`，129 项测试全部通过（约 5 秒），未调用真实远程模型。
