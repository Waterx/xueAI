# LLM Unified Gateway

## 1. 项目介绍

第 21 关最终交付，以第 20 关为基线的 Python LLM 网关。通过一个 `POST /chat` 接口调用 DeepSeek Flash / Pro，提供 SSE、结构化输出、版本化 Prompt、SQLite 指标、统一错误与重试、按模型限流。附带聊天页和观测页。

原有 129 项测试保留，新增 4 项缓存 Token 验收测试（包含多组子用例）。完整结果见 [ACCEPTANCE.md](ACCEPTANCE.md) 与 [evidence/verification.json](evidence/verification.json)。默认测试使用模拟 Provider，不消耗 API Token。

## 2. 技术选型

- Python **3.11+**（使用 `anext`、现代类型标注；验收环境为 **3.13.3**）。
- FastAPI / Uvicorn：HTTP、参数校验、SSE；Pydantic：统一事件与结果。
- Anthropic SDK / OpenAI SDK：分别接入 Messages / Responses 协议。
- HTTPX MockTransport、unittest：离线协议及故障测试；jsonschema：Draft 2020-12 校验。
- SQLite（Python 标准库）：模板版本、调用指标；AnyIO：取消时保护必要清理。
- `requirements.txt` 固定验收使用的直接依赖版本；`requirements.lock.txt` 固定当次验收的完整依赖集合。

## 3. 系统架构

```text
聊天页 / curl
    → FastAPI /chat：请求 ID → 参数与模板校验 → 每模型限流
    → 一次逻辑调用的 Metrics 计时
    → Retry：可重试错误、退避、取消传播
    → model.py：结构化输出验证
    → registry → AnthropicAdapter (Flash) / ResponsesAdapter (Pro)
    → DeepSeek HTTPS
    ← 统一 LLMResult / LLMEvent → JSON / SSE
    → SQLite 指标（每个放行且结束的调用一次入账）
```

| 文件 | 职责 |
| --- | --- |
| `main.py`、`schemas.py` | HTTP 校验、SSE 事件、统一结果、指标生命周期 |
| `model.py`、`adapters/` | 模型路由、协议转换、输出校验、SDK 资源关闭 |
| `errors.py`、`retry.py`、`rate_limiter.py` | 稳定错误、指数退避、滑动窗口 |
| `prompt_manager.py`、`metrics.py` | 模板版本与指标 SQLite |
| `output_monitor.py`、`logging_config.py` | 跨分片异常标记检测及轮转日志 |
| `tests/`、`scripts/verify.py` | 全量回归与最终验收入口 |

`adapters/openai_adapter.py` 保留用于前期 Chat Completions 协议回归，未注册到对外路由；保留它是为了完整运行历史测试。历史 `check_*.py` 临时检查脚本已由统一验收入口替代。

## 4. 环境准备

需要 Python 3.11+、可写的项目目录。真实调用需要有效 `DEEPSEEK_API_KEY`、账户额度和对 `api.deepseek.com` 的 HTTPS 访问；离线测试不需要 Key。

| 配置 | 默认值 / 配置位置 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 进程环境变量，或启动时 `--env-file .env` |
| 本地服务 | `127.0.0.1:8013`；Uvicorn 参数调整 |
| Flash 上游 | `https://api.deepseek.com/anthropic`；适配器常量 |
| Pro 上游 | `https://api.deepseek.com`；适配器常量 |
| SDK 超时 / 输出上限 | 60 秒 / 1024 Token；适配器常量 |
| 重试 | `retry.py: MAX_RETRIES = 3`；SDK 内置重试关闭 |
| 本地配额 | `rate_limiter.py: MODEL_LIMITS`；Flash 5 / Pro 3 次每 60 秒 |
| 数据 | 项目目录下 `prompts.db`、`metrics.db`、`logs/`，与工作目录无关 |

仅 API Key 从环境读取，其他配置不接受同名环境变量。不要把真实 Key、虚拟环境、数据库或业务数据放入交付包。`.env.example` 只含占位符。

## 5. 安装与启动

macOS / Linux，从本目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
cp .env.example .env
# 在本地编辑 .env，填写有效的 DEEPSEEK_API_KEY
python -m uvicorn main:app --env-file .env --host 127.0.0.1 --port 8013 --workers 1
```

Windows PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
# 编辑 .env 后启动
.\.venv\Scripts\python.exe -m uvicorn main:app --env-file .env --port 8013 --workers 1
```

也可以用 `pip install -r requirements.txt` 安装固定的直接依赖，并由 pip 解析间接依赖。进程环境中已设置的 API Key 优先于 `.env`。直接导入模块不会自动读取 `.env`，启动命令已显式加载它。

打开 [聊天页](http://127.0.0.1:8013/)、[观测页](http://127.0.0.1:8013/observability)、[接口文档](http://127.0.0.1:8013/docs)。首次使用自动建库，不需要手工迁移。开发可加 `--reload`，最终验收使用一个 worker。

## 6. API 接口说明

### POST /chat

| 字段 | 说明 |
| --- | --- |
| `model` | `deepseek-flash`（默认）或 `deepseek-v4-pro` |
| `stream` | 默认 `true`；`false` 返回一次 JSON |
| `prompt` | 非空，最多 2000 字符 |
| `messages` | 多轮历史；与 `prompt`、`template_id` **三选一** |
| `template_id` | 已保存模板 ID |
| `template_version` | 仅模板模式可用；正整数，省略 / null 取最新 |
| `variables` | 字符串到字符串的映射，默认 `{}` |
| `conversation_id` | 可选 UUID；省略则生成，仅用于观测关联 |
| `response_format` | 省略为普通文本；`{"type":"json_object"}` 或 `{"type":"json_schema","name":"result","schema":{...}}` |

历史最多 41 条，`user` / `assistant` 交替，首尾为 `user`；单条最多 12000 字符，末条最多 2000，总计最多 60000。不接受客户端 system 消息。会话内容由客户端每次传入，服务端不会用 `conversation_id` 自动恢复历史。

非流式结果：

```json
{"model":"deepseek-flash","content":"你好","finish_reason":"stop","usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10,"input_tokens":8,"output_tokens":2,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}
```

结构化输出的 `content` 仍是字符串，客户端应自行 JSON 解析。普通 Flash 截断可返回 `finish_reason=length`；结构化输出截断视为错误。

SSE 成功序列是 `start → delta* → usage（若有）→ done`。失败以 `error` 终止且不发送 `done`；取消关闭连接。事件格式：

```text
event: delta
data: {"text":"你好"}

event: done
data: {"reason":"stop"}
```

每次 `/chat` 返回 `X-Request-ID`；成功校验请求体后还返回 `X-Conversation-ID`。错误结构为 `{"error":{"code":"...","message":"...","retryable":false,"request_id":"..."}}`；SSE 已开始则 HTTP 保持 200，`event: error` 的 data 直接是内部错误对象。

| 错误码 | HTTP | 可自动重试 |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | 否 |
| `RATE_LIMIT_EXCEEDED` | 429 | 否 |
| `UPSTREAM_AUTH_ERROR` | 502 | 否 |
| `UPSTREAM_RATE_LIMITED` | 503 | 是 |
| `UPSTREAM_TIMEOUT` | 504 | 是 |
| `UPSTREAM_UNAVAILABLE` | 503 | 是 |
| `STRUCTURED_OUTPUT_INVALID` | 502 | 否 |
| `UPSTREAM_BAD_RESPONSE` | 502 | 否 |
| `INTERNAL_ERROR` | 500 | 否 |

`retryable` 表示故障类别，并非当前 SSE 一定可以重试。

### 其他接口

- `POST /templates`：`{"template_id":"greeting","content":"Hello {{name}}"}`，返回 ID 与新版本号。每次保存递增，包括内容相同的情况。
- `GET /metrics`：按模型分组汇总；没有调用记录时为 `{}`。
- `GET /metrics/requests?limit=50`：最近调用，`limit` 为 1–200；`before_id` 使用上一页最后一条 ID 继续翻页。
- 两个 Metrics 接口均支持 UUID 查询参数 `request_id`、`conversation_id`，可组合。
- `/templates`、Metrics 参数错误使用 FastAPI 标准校验响应；统一错误契约针对 `/chat`。

## 7. 功能介绍

### 双协议模型路由与 SSE

Flash 使用 Anthropic Messages；Pro 使用 OpenAI Responses，两者都请求关闭思考模式。内部事件与 SSE 编码分离。正常、失败、取消、显式关闭时均关闭 SDK 及流资源；服务端不向浏览器暴露 API Key。

### JSON 结构化输出

`json_object` 要求根节点为对象；`json_schema` 使用 Draft 2020-12。Flash 通过提示词要求格式，Pro 传 `text.format`，最后都在网关本地验证。拒绝非法 JSON、NaN / Infinity、不符合 Schema、截断或异常终止。

**流式结构化 JSON 在最终验证之前可能已经向客户端发送部分增量。** 客户端必须等到 `done` 才认定本轮成功，遇到 `error` 应丢弃或标记已有部分内容。

### Prompt 模板与版本管理

使用 `{{name}}` 占位符，缺少变量报错，多余变量忽略，替换值不递归解释。模板渲染后同样限制为非空且最多 2000 字符。默认提供 `translate`、`summarize` 模板。SQLite 复合主键与写事务保证并发保存的版本不重复，历史版本可直接调用。

### 可观测性与 Token 口径

每次放行请求记录一次模型层结果（`success` / `error` / `cancelled`），延迟包含重试与退避；参数校验、模板读取、限流检查在计时范围之外。TTFT 为首个非空文本增量的时间，未测到为 null，均值只使用已测样本。

Flash 的三个输入分类互斥：

```text
prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
total_tokens = prompt_tokens + output_tokens
```

原始字段及嵌套缓存分类保留在统一 `usage` 中，由既有 `usage_json` 原样保存；嵌套创建明细不再次求和。任一输入分类缺失 / null 时，`prompt_tokens` 与 `total_tokens` 为 null，已知字段照常保留；**未知不填零**，供应商明确返回的零保留。

流式使用 SDK 合并后的最终累计用量：最终字段覆盖初始字段，不能把两次快照相加。固定的 Anthropic SDK 版本支持最终 input/cache 字段覆盖。Pro 的 `input_tokens` 已是总输入，不另加缓存子集。没有用量时为 null。SQLite 只累计已知值；汇总显示 0 不证明实际没有 Token 消耗。旧历史记录不重新计算，因此跨版本汇总可能包含旧口径数据。

口径依据：[DeepSeek Messages 用量说明](https://api-docs.deepseek.com/guides/anthropic_api/)、[Responses 用量说明](https://api-docs.deepseek.com/guides/responses_api/)。实际字段的脱敏验收见证据文件。

### 统一错误与自动重试

**最多重试 3 次，即首次调用加 3 次重试、最多 4 次上游尝试**。退避 `min(5, 0.5 × 2^(n-1)) + U(0, 0.2)` 秒，当前三次约为 0.5、1、2 秒加抖动。SDK 重试关闭，避免叠加。

只重试临时故障；任何内部流事件一旦转交调用方（包括空 delta、usage、done）就停止自动重试。取消会打断等待；每次失败先关闭旧流再重建。错误响应隐藏上游私有异常内容。

### 按模型限流

**单进程、单事件循环的内存 60 秒滑动窗口**：Flash 5 次、Pro 3 次，独立 deque 与锁保护清理、检查和登记；调用 Provider 时不持锁。校验和模板解析成功后扣一次额度，内部重试不重复扣；失败、取消仍占用，校验失败和本地拒绝不占用。

超限返回 HTTP 429、`RATE_LIMIT_EXCEEDED`、`retryable=false`、`Retry-After`（剩余秒数向上取整），不发送 SSE start、不调用 Provider、不产生模型调用指标。上游 429 则是 HTTP 503 / `UPSTREAM_RATE_LIMITED`。重启清空额度，多个 worker 不共享配额。

## 8. curl 使用示例

以下为 Bash / zsh 示例。PowerShell 建议将 JSON 保存到 UTF-8 文件，再用 `curl.exe --data-binary @request.json`。示例会消耗真实 Token；同一模型连续运行容易达到配额，请按 `Retry-After` 等待后继续。

```bash
BASE=http://127.0.0.1:8013

# Flash 普通非流式；-i 可查看请求 ID
curl -i "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","prompt":"请用一句话介绍你自己","stream":false}'

# Pro 普通非流式
curl "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","prompt":"只回答 OK","stream":false}'

# Flash SSE，-N 禁用 curl 输出缓冲
curl -N "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","prompt":"数到三","stream":true}'

# Pro SSE
curl -N "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","prompt":"数到三","stream":true}'

# JSON Object（Flash / Pro 均支持）
curl "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","prompt":"返回 JSON 对象，包含 name 和 age","stream":false,"response_format":{"type":"json_object"}}'

# JSON Schema；改 stream:true 并加 -N 即可流式验证
curl "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","prompt":"返回 {\"ok\":true}","stream":false,"response_format":{"type":"json_schema","name":"result","schema":{"type":"object","properties":{"ok":{"const":true}},"required":["ok"],"additionalProperties":false}}}'

# 创建模板版本 1，再保存版本 2（请使用一个新的 ID）
curl "$BASE/templates" -H 'Content-Type: application/json' \
  -d '{"template_id":"final_demo","content":"请用中文向{{name}}问好"}'
curl "$BASE/templates" -H 'Content-Type: application/json' \
  -d '{"template_id":"final_demo","content":"请用英语向{{name}}问好"}'

# 精确调用旧版本；省略 template_version 使用最新版
curl "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","template_id":"final_demo","template_version":1,"variables":{"name":"小明"},"stream":false}'

# 多轮：服务端不会自动补历史
curl "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"我叫小明"},{"role":"assistant","content":"你好，小明"},{"role":"user","content":"我叫什么？"}],"stream":false}'

curl "$BASE/metrics"
curl "$BASE/metrics/requests?limit=5"
# 将实际响应头中的 UUID 填入 REQUEST_ID
REQUEST_ID=replace-with-response-request-id
curl "$BASE/metrics/requests?request_id=$REQUEST_ID"

# 参数错误：422 / INVALID_REQUEST，不消耗 Token 和额度
curl -i "$BASE/chat" -H 'Content-Type: application/json' \
  -d '{"model":"unknown","prompt":"hi","stream":false}'

# 在空窗口内连续请求六次，至少第六次触发 Flash 本地 429。
# 前五次最多会真实调用上游，故障自动重试可能产生额外消耗。
# 可控、零消耗的限流证据请运行 verify.py。
for i in 1 2 3 4 5 6; do
  curl -i "$BASE/chat" -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-flash","prompt":"OK","stream":false}'
done
```

最后一个示例假设六次请求落在同一 60 秒窗口内；真实上游过慢时不保证触发，确定性验收使用模拟时钟与并发测试。不要用真实 Provider 偶发故障验证重试。

## 9. 自动化测试

在本目录、安装好依赖后运行：

```bash
python -m unittest discover -s tests -v
python scripts/verify.py
python scripts/verify.py --report /tmp/gateway-verification.json

# 可选：离线测试通过后，四次真实请求（两模型各一次普通、一次 SSE Schema）
python scripts/verify.py --live --report live_acceptance.json
# 显式指定已有环境文件，或事先设置进程环境变量
python scripts/verify.py --live --env-file /path/to/local.env
```

`verify.py` 从任何工作目录运行均可；默认屏蔽真实网络连接，复用全部 unittest，按 14 组汇总，每个测试只运行一次。失败、加载错误、跳过或缺少验收分组均返回非零；`--live` 缺 Key 或任一真实检查失败也返回非零。报告只包含环境版本、结果及合成冒烟请求，不记录 Key。

离线验证覆盖双协议 HTTP、结构化校验、模板并发版本、SQLite 持久化、Token 分类、Latency / TTFT、可控故障重试、并发限流、取消与资源关闭。脚本使用临时指标 / 模板库；模块导入可能创建空的本地指标库与日志目录。单独运行原有 unittest 可能产生模拟指标 / 日志，建议在独立交付副本运行，勿与真实服务共用运行目录。

`--live` 经进程内 ASGI 调用完整 `/chat` 链路，并向真实 Provider 发 HTTPS 请求。SSE 内容和终止事件会检查，但 HTTPX ASGITransport 会缓冲响应，因此它不是网络逐块到达时延的证明；TTFT 是网关收到上游首个文本增量的时间。浏览器 / `curl -N` 可用于实际 TCP 流式检查。

## 10. 已知限制

- 这是可复现的教学工程，不含网关用户鉴权、租户隔离、分布式配额、任务队列或生产部署配置；默认绑定本机。
- 限流仅单进程有效；SDK 客户端每次调用新建并关闭，未做长连接池生命周期优化。
- SDK 60 秒是网络阶段超时，不是整个请求总时限；重试会延长总耗时，live 验收另设每请求 90 秒上限。
- 流式结构化增量在最终验证前不可视为有效 JSON；停止连接不保证供应商停止计费。
- 只支持文本 user / assistant 对话，没有工具调用、图像输入或服务端历史会话存储。
- Pro 目前保留统一用量总数；Flash 额外保留上报的原始分类。供应商没有上报的分类与失败尝试用量无法推算。
- SQLite 历史不重算；磁盘写入失败会记录日志，但可能缺少该次指标。数据文件需自行备份。
- 异常输出日志会保存少量输出片段，属于可能敏感的运行数据；日志轮转为 2 MiB × 主文件及 3 个备份，已排除交付。
- 真实模型可用性、额度和格式遵循程度由 Provider 决定；正常冒烟不证明故障机制正确，故障机制以模拟验收为准。
