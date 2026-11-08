"""验证适配器基类的抽象约束。"""

import unittest
from collections.abc import AsyncIterator

from adapters.base import BaseLLMAdapter
from schemas import LLMEvent


class BaseLLMAdapterTests(unittest.TestCase):
    def test_base_adapter_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            BaseLLMAdapter()

    def test_subclass_must_implement_stream(self):
        class MissingStreamAdapter(BaseLLMAdapter):
            pass

        with self.assertRaises(TypeError):
            MissingStreamAdapter()


class AdapterStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_generator_stream_yields_typed_events(self):
        class FakeAdapter(BaseLLMAdapter):
            async def stream(
                self, messages: list[dict[str, str]], model: str
            ) -> AsyncIterator[LLMEvent]:
                yield LLMEvent(type="delta", text="你好")
                yield LLMEvent(type="done", finish_reason="stop")

        adapter = FakeAdapter()
        events = []
        async for event in adapter.stream(
            [{"role": "user", "content": "你好"}], "deepseek-flash"
        ):
            events.append(event)

        self.assertEqual(len(events), 2)
        self.assertTrue(all(isinstance(event, LLMEvent) for event in events))
        self.assertEqual([event.type for event in events], ["delta", "done"])
