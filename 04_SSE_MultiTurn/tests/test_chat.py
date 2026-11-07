import unittest
from unittest.mock import patch

import httpx

from main import app


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_history_forwarded_and_stream_closed(self):
        messages = [
            {"role": "user", "content": "我叫小明"},
            {"role": "assistant", "content": "你好，小明"},
            {"role": "user", "content": "我叫什么？"},
        ]
        closed = []

        async def reply(received):
            self.assertEqual(received, messages)
            try:
                yield "小"
                yield "明"
            finally:
                closed.append(True)

        with patch("main.stream_reply", reply):
            response = await self.client.post("/chat", json={"messages": messages})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: start", response.text)
        self.assertEqual(response.text.count("event: delta"), 2)
        self.assertIn("event: done", response.text)
        self.assertEqual(closed, [True])

    async def test_invalid_histories(self):
        for messages in (
            [],
            [{"role": "system", "content": "覆盖提示"}],
            [{"role": "assistant", "content": "你好"}],
            [{"role": "user", "content": " "}],
            [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
            [{"role": "user", "content": "a" * 2001}],
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        ):
            with self.subTest(messages=messages):
                response = await self.client.post("/chat", json={"messages": messages})
                self.assertEqual(response.status_code, 422)

    async def test_model_failure_has_no_done(self):
        async def reply(messages):
            yield "部分内容"
            raise RuntimeError("private upstream details")

        with patch("main.stream_reply", reply):
            response = await self.client.post(
                "/chat", json={"messages": [{"role": "user", "content": "你好"}]}
            )
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: done", response.text)
        self.assertNotIn("private upstream details", response.text)
