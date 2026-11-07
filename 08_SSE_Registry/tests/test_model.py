import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from model import stream_reply
from schemas import LLMEvent


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, reason="stop", include_usage=True):
        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(body["stream_options"], {"include_usage": True})
            self.assertEqual(body["model"], "deepseek-flash")
            base = {"id": "test", "object": "chat.completion.chunk", "created": 0,
                    "model": "deepseek-flash"}
            chunks = [
                {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你好"}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            ]
            if include_usage:
                chunks.append({**base, "choices": [], "usage": {
                    "prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10,
                }})
            wire = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(200, text=wire + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        client = AsyncOpenAI(api_key="test", http_client=http_client)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch("adapters.openai_adapter.AsyncOpenAI", return_value=client):
            events = [event async for event in stream_reply([{"role": "user", "content": "你好"}], "deepseek-flash")]
        self.assertTrue(http_client.is_closed)
        self.assertTrue(all(isinstance(event, LLMEvent) for event in events))
        return events

    async def test_sdk_stream_becomes_typed_events(self):
        events = await self.collect()
        self.assertEqual([event.type for event in events], ["delta", "usage", "done"])
        self.assertEqual(events[0].text, "你好")
        self.assertEqual(events[1].usage, {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10})
        self.assertEqual(events[2].finish_reason, "stop")

    async def test_length_is_a_done_reason(self):
        events = await self.collect(reason="length")
        self.assertEqual(events[-1].finish_reason, "length")

    async def test_missing_usage_is_not_fabricated(self):
        events = await self.collect(include_usage=False)
        self.assertEqual([event.type for event in events], ["delta", "done"])


class SchemaTests(unittest.TestCase):
    def test_invalid_event_payloads(self):
        for payload in ({"type": "unknown"}, {"type": "delta"},
                        {"type": "done"}, {"type": "usage"},
                        {"type": "delta", "text": "hi", "finish_reason": "stop"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                LLMEvent(**payload)
