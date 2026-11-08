"""模型适配器的统一接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schemas import LLMEvent


class BaseLLMAdapter(ABC):
    @abstractmethod
    def stream(
        self, messages: list[dict[str, str]], model: str
    ) -> AsyncIterator[LLMEvent]:
        """返回内部事件的异步迭代器；子类可使用异步生成器实现。"""
        raise NotImplementedError
