"""真实 SDK + 模拟 SSE 验证结构化流及资源释放，不访问网络。"""

from rate_limiter import RateLimiter
import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from main import app
from model import stream_reply
from test_anthropic_adapter import AnthropicWire
from test_responses_adapter import ResponsesWire
from test_json_schema import FORMAT


class StreamingJSONTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))

    async def exercise(self, pro, fmt, content='{"name":"张三","age":30}', failure=None, mode=None):
        options = {"texts": [content[:8], content[8:]]}
        if failure == "length":
            options.update({"terminal": "response.incomplete", "status": "incomplete"} if pro else {"reason": "max_tokens"})
        elif failure == "upstream":
            options["fail"] = True
        elif failure == "missing":
            options.update({"terminal": None} if pro else {"truncate": True})
        if mode:
            options["block"] = True
        wire = ResponsesWire(**options) if pro else AnthropicWire(**options)
        model = "deepseek-v4-pro" if pro else "deepseek-flash"
        def handle(request):
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            for field in ("tools", "tool_choice", "output_config", "response_format"):
                self.assertNotIn(field, body)
            if pro:
                self.assertEqual(body["text"], {"format": fmt})
            else:
                self.assertIn("JSON", body["system"])
                if fmt["type"] == "json_schema":
                    self.assertIn(json.dumps(fmt["schema"], ensure_ascii=False), body["system"])
            return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream"})
        sdk = AsyncOpenAI if pro else AsyncAnthropic
        module = "adapters.responses_adapter" if pro else "adapters.anthropic_adapter"
        client = sdk(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(module + "." + sdk.__name__, return_value=client):
            if mode:
                received = []
                events = stream_reply([{"role": "user", "content": "JSON"}], model, fmt)
                try:
                    if mode == "close":
                        received.append(await anext(events))
                    else:
                        async def consume():
                            async for event in events:
                                received.append(event)
                        task = asyncio.create_task(consume())
                        try:
                            await asyncio.wait_for(wire.waiting.wait(), 2)
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                finally:
                    await events.aclose()
                self.assertNotIn("done", [e.type for e in received])
                result = None
            else:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
                    response = await browser.post("/chat", json={"model": model, "prompt": "JSON", "stream": True, "response_format": fmt})
                self.assertEqual(response.status_code, 200)
                result = [(frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:])) for frame in response.text.strip().split("\n\n")]
        self.assertTrue(wire.closed)
        self.assertTrue(client.is_closed())
        return result

    async def test_four_combinations(self):
        for pro in (False, True):
            for fmt in ({"type": "json_object"}, FORMAT):
                with self.subTest(pro=pro, fmt=fmt["type"]):
                    events = await self.exercise(pro, fmt)
                    self.assertEqual([e[0] for e in events], ["start", "delta", "delta", "usage", "done"])
                    self.assertEqual(json.loads(''.join(e[1]["text"] for e in events if e[0] == "delta")), {"name": "张三", "age": 30})

    async def test_failures_never_done(self):
        for pro in (False, True):
            for fmt in ({"type": "json_object"}, FORMAT):
                for content, failure in [('{"name":', None), ('[]', None), ('{"age":NaN}', None), ('{}', 'length'), ('{}', 'upstream'), ('{}', 'missing')]:
                    with self.subTest(pro=pro, fmt=fmt, content=content, failure=failure):
                        events = await self.exercise(pro, fmt, content, failure)
                        self.assertEqual(events[-1][0], "error")
                        self.assertNotIn("done", [e[0] for e in events])
            for content in ('{}', '{"name":"张三","age":"30"}', '{"name":"张三","age":30,"extra":1}'):
                events = await self.exercise(pro, FORMAT, content)
                self.assertEqual(events[-1][0], "error")
                self.assertNotIn("done", [e[0] for e in events])

    async def test_cancel_and_close(self):
        for pro in (False, True):
            for fmt in ({"type": "json_object"}, FORMAT):
                for mode in ('cancel', 'close'):
                    await self.exercise(pro, fmt, mode=mode)

    async def test_invalid_stream_schema_rejected_before_adapter(self):
        with patch('model.get_adapter') as adapter:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as browser:
                response = await browser.post('/chat', json={'prompt': 'JSON', 'stream': True, 'response_format': {**FORMAT, 'schema': {'type': 'invalid'}}})
            self.assertEqual(response.status_code, 422)
            adapter.assert_not_called()
