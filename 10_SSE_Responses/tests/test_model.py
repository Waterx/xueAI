"""验证当前 Anthropic 路由的模型入口、事件转发与统一事件校验。"""

import unittest
from unittest.mock import patch

from adapters.anthropic_adapter import AnthropicAdapter
from adapters.base import BaseLLMAdapter
from model import stream_reply
from schemas import LLMEvent


class ModelEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_entry_forwards_arguments_and_events(self):
        adapter = AnthropicAdapter()
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

        with (
            patch("model.get_adapter", return_value=adapter) as route,
            patch.object(adapter, "stream", side_effect=fake_stream) as upstream,
        ):
            events = [event async for event in stream_reply(messages, "deepseek-flash")]

        route.assert_called_once_with("deepseek-flash")
        upstream.assert_called_once_with(messages, "deepseek-flash")
        self.assertEqual(events, expected)
        self.assertTrue(all(isinstance(event, LLMEvent) for event in events))
        self.assertEqual([event.type for event in events], ["delta", "done"])
        self.assertEqual(closed, [True])


class SchemaTests(unittest.TestCase):
    def test_invalid_event_payloads(self):
        for payload in ({"type": "unknown"}, {"type": "delta"},
                        {"type": "done"}, {"type": "usage"},
                        {"type": "delta", "text": "hi", "finish_reason": "stop"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                LLMEvent(**payload)
