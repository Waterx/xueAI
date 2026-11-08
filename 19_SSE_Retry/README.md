# 第 19 关 · 指数退避重试

Flash（Anthropic Messages）和 Pro（OpenAI Responses）共用 `POST /chat` 的错误契约。请求可通过第 17 关的 SQLite 指标定位。


## 本关实现

- 新增 `retry.py`，非流式使用 `with_retry`，流式使用 `stream_with_retry`。
- 最多重试 3 次，共最多 4 次 Provider 调用。每次创建新调用及新流，SDK 保持 `max_retries=0`。
- 等待为 `min(5, 0.5 * 2 ** (n - 1)) + uniform(0, 0.2)` 秒，前三次基础等待为 0.5、1、2 秒。使用可取消的 `asyncio.sleep`。
- 仅对 `normalize_error` 判定可重试的超时、限流、服务不可用重试。上游 429 仍映射为 HTTP 503。
- SSE `start` 只发送一次；模型事件交给 HTTP 层之后（包含空 delta、usage、done）不再重试。重试只捕获上游创建和迭代异常。
- 每个失败流在等待前关闭。取消立即传播；关闭或下游发送失败不触发重新调用。
- `done` 暂存到迭代、资源清理完成后发送，保证它是最后一个事件；后到的 Usage 仍保留。失败时只发送 `error`。
- 一次请求只落一条指标，延迟和 TTFT 包含重试等待，Usage 只记录实际收到的数据。
- 补齐 Provider `timeout_error` 映射。不引入新依赖、熔断器或限流机制。

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

### 第 19 关验收结果

2026-11-08：运行 `../.venv-mac/bin/python -m unittest discover -s tests -v`，120 项测试全部通过（约 5 秒）。未调用真实远程模型。

`tests/test_retry.py` 覆盖 T1–T12：首次成功、503 后恢复、四次失败上限、鉴权与结构化输出不重试、429 与超时重试、SSE 首事件前恢复及首事件后停止、等待取消、失败流关闭、单条指标和包含等待的计时。另覆盖退避抖动、空 delta/usage/done 的重试边界、下游及清理异常不重试、done 后上游失败。

原有 `tests/test_complete.py` 使用真实 SDK 和 MockTransport，验证两种协议每次重试创建新客户端并全部关闭；`tests/test_metrics.py` 更新为最终 done 位于 usage 之后。
