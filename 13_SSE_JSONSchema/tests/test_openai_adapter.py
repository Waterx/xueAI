import os
from collections.abc import AsyncIterator
from unittest.mock import patch

from adapters.base import BaseLLMAdapter
from adapters.openai_adapter import OpenAIAdapter
from schemas import LLMEvent
from test_openai_model import ModelTests as _ModelTests


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
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                async for _ in OpenAIAdapter().stream([], "deepseek-flash"):
                    pass


del _ModelTests
