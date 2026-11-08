# 09：实现 AnthropicAdapter

基于 `08_Registry` 创建，通过 DeepSeek 的 Anthropic Messages 兼容接口，
学习第二种 API 协议如何转换为统一的 `LLMEvent`。

`adapters/anthropic_adapter.py` 的 `AnthropicAdapter` 继承原有 `BaseLLMAdapter`，
实现 `stream(messages, model)`，接口与架构保持不变。注册表现在将
`deepseek-flash` 映射到 `AnthropicAdapter`；旧 `OpenAIAdapter` 文件保留，
本关不迁移 Responses API。

## Checklist：10/10

- [x] 安装 Anthropic SDK 并更新 requirements.txt
- [x] 新建 AnthropicAdapter，继承已有 BaseLLMAdapter
- [x] 配置 AsyncAnthropic 密钥、base_url、timeout 和重试次数
- [x] 调用 Messages 流接口，单独传入 system
- [x] 将 text_stream 转为 delta
- [x] 检查最终 Message，正常结束转为 done stop
- [x] 使用双层 async with 管理资源
- [x] 更新 registry，保留旧 OpenAIAdapter
- [x] 更新并运行 Mock 测试
- [x] 通过 /chat 验证真实连续输出

## 实现

- `AsyncAnthropic` 从 `DEEPSEEK_API_KEY` 读取密钥，使用
  `https://api.deepseek.com/anthropic`、`timeout=60.0`、`max_retries=0`。
- `client.messages.stream()` 接收 `model`、`messages`、`max_tokens=1024`；
  原有中文 system 提示单独通过 `system` 参数传入。
- 显式使用 `thinking={"type": "disabled"}`，延续 08 的非思考调用方式。
- 遍历 `stream.text_stream`，经原有输出监测后逐块产生 `delta`。
- `await stream.get_final_message()` 获取最终 Message：`end_turn` 和
  `stop_sequence` 映射为 `done(reason=stop)`；`max_tokens` 映射为
  `done(reason=length)`，避免将截断误报为正常结束。缺失或不支持的结束原因抛错，
  由原有 `/chat` 转换为 `error`，不发送 `done`。
- 最终 Message 的 `input_tokens`、`output_tokens` 转为原有 `usage` 载荷，
  `total_tokens` 为两者之和，仍在 `done` 前发送。
- 客户端和上游流均由 `async with` 管理；`model.py` 沿用 `aclosing`，
  保留异常、取消与主动关闭时的清理能力。

调用链：`POST /chat → stream_reply → get_adapter → AnthropicAdapter.stream`。
成功事件顺序：`start → delta × N → usage → done`。

## 安装与测试

在工作区根目录执行：

```sh
.venv-mac/bin/python -m pip install -r 09_Anthropic/requirements.txt
cd 09_Anthropic
../.venv-mac/bin/python -m unittest discover -s tests -v
```

本机已在 `.venv-mac` 安装 Anthropic SDK 0.125.0。
47 项测试全部通过；自动测试全部使用 Mock，不调用真实 API。

`tests/test_anthropic_adapter.py` 使用真实 SDK 配合 `httpx.MockTransport`，
验证请求地址、密钥参数、system 分离、连续文本、用量、结束原因、资源释放和完整
`/chat` SSE 路径。`tests/test_registry.py` 已更新注册表断言。
旧 OpenAI 协议测试在 `tests/test_openai_model.py` 中直接调用旧 Adapter，
不依赖默认注册表。`tests/test_model.py` 使用 AnthropicAdapter，并在
`model.get_adapter` 处 Mock，验证参数、事件转发和关闭；统一事件校验也在该文件中。
`tests/test_openai_adapter.py` 不再替换模型入口；旧资源关闭测试明确只测旧 OpenAIAdapter，
当前默认路由和 AnthropicAdapter 的资源关闭由 `tests/test_anthropic_adapter.py` 覆盖。

## 启动与真实验收

在终端配置 `DEEPSEEK_API_KEY` 后执行：

```sh
cd 09_Anthropic
../.venv-mac/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8005
```

另开终端，在 09 目录执行（会消耗少量 API 额度）：

```sh
../.venv-mac/bin/python check_adapter.py
```

可用 `--url http://127.0.0.1:8009` 指定服务地址。
验收脚本通过真实 HTTP `/chat` 逐行读取 SSE，打印接收时间，检查
`start`、至少两个文本增量及正常 `done`，遇到 `error` 则失败。

本机真实验收（2026-11-02）：使用临时端口 8009，通过 `/chat` 收到
`start → 35 个 delta → usage → done(reason=stop)`，首个 delta 约 1.10 秒，
正常完成约 1.39 秒。用量为输入 31、输出 35、合计 66 Token。
临时服务读取 07 的现有 `.env`，验收后已关闭。

密钥也可自行放入本目录 `.env`，启动时增加 `--env-file .env`。
本关未复制 08 或 07 的密钥文件及历史日志。

## 官方参考

- [DeepSeek Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)
- [Anthropic SDK 流式辅助接口](https://github.com/anthropics/anthropic-sdk-python/blob/main/helpers.md)
