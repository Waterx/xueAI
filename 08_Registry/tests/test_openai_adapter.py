import unittest
from collections.abc import AsyncIterator
from unittest.mock import patch

from adapters.base import BaseLLMAdapter
from adapters.openai_adapter import OpenAIAdapter
from schemas import LLMEvent
from test_model import ModelTests as _ModelTests


class ModelEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_entry_forwards_arguments_and_events(self):
        adapter = OpenAIAdapter()
        self.assertIsInstance(adapter, BaseLLMAdapter)
        messages = [{"role": "user", "content": "你好"}]
        expected = [
            LLMEvent(type="delta", text="你好"),
            LLMEvent(type="done", finish_reason="stop"),
        ]
        closed = []

        async def fake_stream(messages, model):
            try:
                for event in expected:
                    yield event
            finally:
                closed.append(True)

        from model import stream_reply

        with patch.object(OpenAIAdapter, "stream", side_effect=fake_stream) as upstream:
            events = [event async for event in stream_reply(messages, "deepseek-flash")]

        upstream.assert_called_once_with(messages, "deepseek-flash")
        self.assertEqual(events, expected)
        self.assertTrue(all(isinstance(event, LLMEvent) for event in events))
        self.assertEqual([event.type for event in events], ["delta", "done"])
        self.assertEqual(closed, [True])


class OpenAIAdapterTests(_ModelTests):
    def test_adapter_is_base_adapter(self):
        self.assertIsInstance(OpenAIAdapter(), BaseLLMAdapter)

    async def test_stream_returns_async_iterator(self):
        # 创建生成器不会发起请求；无需密钥即可验证返回对象的协议。
        events = OpenAIAdapter().stream(
            [{"role": "user", "content": "你好"}], "deepseek-flash"
        )
        try:
            self.assertIsInstance(events, AsyncIterator)
            self.assertIs(aiter(events), events)
        finally:
            await events.aclose()

    async def collect(self, reason="stop", include_usage=True):
        # 复用模拟 HTTP 响应，用真实 SDK 直接驱动 Adapter。
        with patch("test_model.stream_reply", OpenAIAdapter().stream):
            return await super().collect(reason, include_usage)

    async def test_normal_events_are_typed_and_ordered(self):
        for include_usage in (True, False):
            with self.subTest(include_usage=include_usage):
                events = await self.collect(include_usage=include_usage)
                self.assertGreater(len(events), 0)
                for event in events:
                    self.assertIsInstance(event, LLMEvent)
                expected = ["delta", "usage", "done"] if include_usage else ["delta", "done"]
                self.assertEqual([event.type for event in events], expected)

    async def test_missing_api_key(self):
        import os

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                async for _ in OpenAIAdapter().stream([], "deepseek-flash"):
                    pass


del _ModelTests
