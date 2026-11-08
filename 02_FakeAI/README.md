# 02：通过 SSE 接入伪 AI

基于 `01_Intro`，把计数推送换成逐段生成的 AI 回答。不调用外部服务，无需 API Key；01 保留作为基础版本。

## 启动

在项目根目录执行，复用已有虚拟环境：

```bash
cd 02_FakeAI
../.venv/bin/python -m uvicorn main:app --reload --port 8001
```

打开 http://127.0.0.1:8001。输入问题，点击发送；点击停止可取消请求。输入 `/error` 可模拟模型失败。

新环境可在本目录创建 `.venv`，安装 `requirements.txt`，再使用本目录的 Python 启动。

## 从 01 到 02，改造了什么

| 位置 | 01 | 02 |
| --- | --- | --- |
| 数据来源 | 每秒生成编号 | `model.py` 逐段生成文本 |
| 接口 | GET /events | POST /chat，JSON 请求体携带 prompt |
| SSE 事件 | tick | start、delta、done、error |
| 前端客户端 | EventSource | fetch + ReadableStream |
| 页面更新 | 打印独立消息 | 将 delta.text 追加为完整回答 |
| 结束方式 | 持续推送直到关闭 | 生成完发 done；也可主动停止 |
| 重连 | EventSource 自动重连 | 不自动重试，避免重复启动生成 |

原生 EventSource 不支持 POST 请求体。本例选择 fetch 提交问题，同时读取这个请求的 SSE 响应。也可以设计成“POST 创建任务，再用 EventSource GET 订阅结果”，但那需要额外管理任务状态。

SSE 的格式没有变化，变化的是前端解析方式。这里的读取器适配本后端输出的 LF 换行和 JSON 数据，不是实现全部 SSE 标准的通用 EventSource 替代品，不支持自动重连、id 和 retry。

## 一次提问的执行流程

```text
前端 POST /chat，携带 {"prompt": "问题"}
    → 后端验证输入（非空、非纯空格、最长 2000 字符）
    → start：开始生成
    → model.stream_reply(prompt) 逐段产出文本
    → delta：推送新增文本，前端追加显示
    → done：回答完成，前端停止读取
```

模型失败时发送 error；请求输入不合法时，在建立流之前返回 HTTP 422。流开始后，错误通过 SSE 事件传递，因为 HTTP 响应头已经发送。

示例事件：

```text
event: delta
data: {"text": "你好"}

```

网络读取的一块可能包含半条或多条事件。前端用缓冲区等到空行再解析；TextDecoder 的流式模式处理 UTF-8 字符跨块的情况。回答通过 textContent 写入页面。

## 接入真实模型时改哪里

主要替换 `model.py` 的 `stream_reply(prompt)`：

```python
async def stream_reply(prompt):
    # 在此调用模型 SDK 的异步流式接口。
    # 从每个上游事件中提取“本次新增文本”。
    # yield text
    ...
```

保持“异步迭代器逐段返回 str”的约定，`main.py` 和前端就可以继续使用。应返回新增文本，若上游返回累计全文，需要先提取差量，否则页面会重复追加。

API Key 从后端环境变量读取；具体 SDK、模型名、超时及错误处理放在适配层。使用异步上下文管理器关闭上游模型流，让请求取消时也能释放连接。取消本地请求并不保证所有模型服务立即停止远程计算，需核对提供方的行为。

## 停止与边界

“停止生成”调用 AbortController.abort() 关闭请求。后端框架终止流式响应，finally 关闭本次模型生成器。本例没有额外创建独立后台任务。

伪 AI 的四字符分段仅用于模拟流式效果，不代表真实模型 token。每次提问相互独立，没有对话历史、持久化或断点续传。

## 命令行体验

```bash
curl -N http://127.0.0.1:8001/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"你好"}'
```

