# 04：SSE 多轮对话

从 03_SSE_DeepSeek 升级，保留独立的 03 项目。继续使用 DeepSeek、POST /chat 和 start / delta / done / error 事件。

## Windows 启动

在 xueAI 目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r 04_SSE_MultiTurn/requirements.txt
cd 04_SSE_MultiTurn
..\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8003
```

打开 http://127.0.0.1:8003 。后端沿用系统环境变量 `DEEPSEEK_API_KEY`。设置后需重新启动终端以继承环境变量。

## 多轮对话如何工作

1. 浏览器在 `history` 数组保存成功完成的 user / assistant 消息。
2. 每次请求发送历史加上当前问题，格式为 `{"messages":[{"role":"user","content":"我叫小明"},{"role":"assistant","content":"你好，小明！"},{"role":"user","content":"我叫什么？"}]}`。
3. 后端校验消息角色交替、最后一条为 user，在模型适配层添加固定 system 提示，再把完整消息传给模型。
4. SSE delta 只更新当前助手气泡；收到 done 后，才把本轮问答加入历史。
5. 停止、断线、模型错误或截断时保留已显示文本，本轮不进入上下文。输入框保留原问题以便重新发送。

“新对话”清空历史、界面和事件日志。生成期间禁止重复提交和新对话；不同页面分别维护自己的历史，服务端不存储会话。刷新页面会丢失记录。

每个页面最多完成 20 轮；接口最多接收 41 条消息，每条最多 12000 字符、当前问题最多 2000 字符、请求消息内容合计最多 60000 字符。达到限制时开始新对话，不会静默丢弃旧消息。每次最多输出 1024 tokens，超限会走错误流程。字符上限不等于模型 token 上限。

## 验证

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

自动测试替换模型流，不消耗 API 额度。手动验证可先说“我叫小明”，再问“我叫什么？”，并检查第二次请求包含前一轮问答。再验证停止生成后重新发送，以及新对话清空上下文。

真实请求会使用 API 额度，历史会随每轮一起发送；停止本地连接不保证上游立即停止计算或计费。沿用 03 的教学日志，会在后端打印 SDK 事件和回答内容。
