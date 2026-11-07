# 07：模型适配器抽象接口

基于 06_SSE_Events，新增 `adapters/base.py`，定义继承 `ABC` 的
`BaseLLMAdapter`。抽象方法 `stream(messages, model)` 的返回类型为
`AsyncIterator[LLMEvent]`，子类可用异步生成器实现。

`adapters/openai_adapter.py` 直接执行 DeepSeek Flash 流式调用，负责密钥、
客户端配置、SDK 事件转换、结束原因及 Token 用量。`model.py` 仅保留
委托适配器的兼容入口，`main.py` 保持不变。
基类和未实现 `stream` 的子类均不能直接实例化。

## 验证

在工作区根目录执行（模拟上游，不消耗 API 额度）：

```sh
cd 07_SSE_Adapter
../.venv-mac/bin/python -m unittest discover -s tests -v
```

请使用 Python 3.10+ 且已安装 `requirements.txt` 依赖的环境。仅验证抽象约束可运行：

```sh
../.venv-mac/bin/python -m unittest discover -s tests -p test_adapter.py -v
```

当前 Mac 已配置工作区根目录的 `.venv-mac`，使用 Python 3.13.3，与 Windows 原环境一致。启动服务：

```sh
cd 07_SSE_Adapter
../.venv-mac/bin/python -m uvicorn main:app --env-file .env --host 127.0.0.1 --port 8005
```

## 真实 API 验收

在终端中安全配置 `DEEPSEEK_API_KEY` 后，在 07 目录运行（会消耗少量额度）：

```sh
../.venv-mac/bin/python check_adapter.py
```

脚本直接调用 `OpenAIAdapter().stream()`，打印内部事件，并检查收到文字和正常结束事件。
用量事件仅在上游实际返回时产生。无密钥时明确报错。

## 资源管理与输出监测

Adapter 保留客户端和 SDK 流的两层 `async with`；`model.py` 使用
`aclosing` 将关闭传播到 Adapter。`OutputMonitor.check()` 在每个文本增量
发出前执行，跨分片异常标记仍会触发原有诊断异常，且不发送 `done`。

`tests/test_adapter_resources.py` 使用真实 SDK 与模拟 HTTP 字节流，分别对
Adapter 和兼容入口验证正常结束、上游读取异常、跨分片输出异常、任务取消、
显式关闭时的资源释放，同时断言 HTTP 响应流与客户端均已关闭。

本机密钥保存在 07 目录的 `.env`（已加入 Git 忽略规则）。启动时使用
`--env-file .env` 加载；不要共享该文件。
