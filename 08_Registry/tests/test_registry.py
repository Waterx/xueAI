import unittest
from unittest.mock import patch

from adapters.base import BaseLLMAdapter
from adapters.openai_adapter import OpenAIAdapter
from adapters.registry import ADAPTERS, get_adapter
from model import stream_reply
from schemas import LLMEvent


class RegistryTests(unittest.TestCase):
    def test_registered_model_returns_adapter_instance(self):
        self.assertIs(ADAPTERS["deepseek-flash"], OpenAIAdapter)
        adapter = get_adapter("deepseek-flash")
        self.assertIsInstance(adapter, OpenAIAdapter)
        self.assertIsInstance(adapter, BaseLLMAdapter)
        self.assertIsNot(adapter, get_adapter("deepseek-flash"))

    def test_unknown_model_raises_clear_value_error(self):
        with self.assertRaisesRegex(ValueError, "不支持的模型.*unknown"):
            get_adapter("unknown")


class RegistryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_entry_uses_registered_adapter(self):
        messages = [{"role": "user", "content": "你好"}]
        received = []
        closed = []
        expected = [LLMEvent(type="delta", text="你好"),
                    LLMEvent(type="done", finish_reason="stop")]

        class TestAdapter(BaseLLMAdapter):
            async def stream(self, messages, model):
                received.append((messages, model))
                try:
                    for event in expected:
                        yield event
                finally:
                    closed.append(True)

        with patch.dict(ADAPTERS, {"test-model": TestAdapter}):
            events = [event async for event in stream_reply(messages, "test-model")]

        self.assertEqual(received, [(messages, "test-model")])
        self.assertEqual(events, expected)
        self.assertEqual(closed, [True])

    async def test_model_entry_rejects_unknown_model(self):
        with self.assertRaisesRegex(ValueError, "不支持的模型.*unknown"):
            async for _ in stream_reply([], "unknown"):
                self.fail("未知模型不应产生事件")
