"""根据模型名称选择并实例化适配器。"""

from adapters.base import BaseLLMAdapter
from adapters.anthropic_adapter import AnthropicAdapter


ADAPTERS: dict[str, type[BaseLLMAdapter]] = {
    "deepseek-flash": AnthropicAdapter,
}


def get_adapter(model: str) -> BaseLLMAdapter:
    adapter_class = ADAPTERS.get(model)
    if adapter_class is None:
        raise ValueError(f"不支持的模型：{model!r}")
    return adapter_class()
