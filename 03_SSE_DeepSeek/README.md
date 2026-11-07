# 03：SSE 接入真实 DeepSeek Flash

教学讨论整理见 [03 学习笔记](03学习笔记.md)，涵盖异步语法、SDK 事件、snapshot、传输顺序与截断实验。

沿用 02_SSE_FakeAI 的 POST /chat 和 start、delta、done、error 协议。
后端从系统环境变量 DEEPSEEK_API_KEY 读取密钥，不写入前端或文件。

## Windows 启动

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r 03_SSE_DeepSeek/requirements.txt
cd 03_SSE_DeepSeek
..\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8002
```

打开 http://127.0.0.1:8002 。设置系统环境变量后，需要新启动终端/应用才能继承。
真实请求会使用 DeepSeek API 额度。

## 相比 02 的升级

- model.py 使用 AsyncOpenAI 连接 https://api.deepseek.com，模型为 deepseek-flash。
- 固定模板与人工 sleep 改为 SDK 异步流，只转发 content.delta 新增文本。
- 关闭 thinking，专注展示最终回答；当前每次请求最多输出 256 tokens（曾用 64 做截断实验）。
- 设置 60 秒网络超时，不自动重试，避免重复启动生成。
- 上游异常转为 SSE error，错误日志记录异常类型；教学观察另会打印完整 SDK 事件，包含回答文本与累计快照。
- 使用异步上下文管理器释放上游流；浏览器停止时关闭本地请求与上游连接。
- 非正常结束（如输出达到上限）显示错误并保留已生成内容。
- 前端解析和 textContent 追加机制保持一致；start 中的模型名改为真实模型。

仍为单轮问答，无历史记录。不再提供 /error 模拟指令。
停止本地连接不保证服务端立即停止计算或计费。

官方文档：https://api-docs.deepseek.com/quick_start/agent_integrations/
