# 08：模型注册表与动态路由

基于 `07_Adapter` 创建，新增 `adapters/registry.py`，保留原有 Adapter 接口、
SSE 事件和资源关闭逻辑。

`ADAPTERS` 保存模型名到 Adapter 类的映射，目前仅注册
`deepseek-flash → OpenAIAdapter`。`get_adapter(model: str) -> BaseLLMAdapter`
每次实例化并返回对应适配器；未知模型抛出包含模型名的 `ValueError`。
`model.py` 通过 `get_adapter(model)` 获取实例，再用原有 `aclosing` 转发事件和关闭。

## 本关 Checklist

- [x] 新建 `adapters/registry.py`
- [x] 定义保存 Adapter 类的 `ADAPTERS` 字典
- [x] 实现 `get_adapter(model: str) -> BaseLLMAdapter`
- [x] 未知模型抛出明确的 `ValueError`
- [x] `model.py` 改为通过注册表获取适配器
- [x] 保留 start、delta、usage、done、error 及资源关闭逻辑
- [x] 添加正常、异常及动态路由验证
- [x] 运行原有测试与新增测试

## 验证

在工作区根目录执行（模拟上游，不消耗 API 额度）：

```sh
cd 08_Registry
../.venv-mac/bin/python -m unittest discover -s tests -v
```

仅验证注册表与路由：

```sh
../.venv-mac/bin/python -m unittest discover -s tests -p test_registry.py -v
```

需使用 Python 3.10+ 并安装 `requirements.txt`。当前工作区的 `.venv-mac` 已配置依赖。
原有 SDK Mock 路径 `adapters.openai_adapter.AsyncOpenAI` 和适配器方法 Mock
仍有效。新增路由测试临时注册另一个 Adapter，验证入口按模型名选择、转发参数和事件、
正常关闭，并检查未知模型异常。

## 启动

先在终端配置 `DEEPSEEK_API_KEY`，再执行：

```sh
cd 08_Registry
../.venv-mac/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8005
```

也可在本目录自行配置 `.env`，启动命令添加 `--env-file .env`。
本关未复制 07 的密钥文件和运行日志。

## 路由与 SSE 行为

调用链：`POST /chat → stream_reply() → get_adapter(model) → adapter.stream()`。
HTTP 输入仍只接受 `deepseek-flash`，非法模型保持返回 422；直接调用注册表或
模型入口时，未知模型抛出 `ValueError`。

成功事件顺序仍为 `start → delta × N → usage（可选）→ done`。
异常通过现有 `error` 事件返回；输出监测和上游资源释放保持原有行为。
`tests/test_adapter_resources.py` 继续验证正常结束、读取异常、输出异常、取消和显式关闭。
完整请求过程见 [请求流程图](请求流程图.md)。

本关不接入 Anthropic，不修改 `BaseLLMAdapter`，不引入工厂框架、依赖注入或实例复用。
