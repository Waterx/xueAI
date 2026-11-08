from rate_limiter import RateLimiter
import json
import unittest
from unittest.mock import patch

import httpx

from main import ChatInput, app
from schemas import LLMEvent
from output_monitor import OutputAnomalyError


class ChatInputTests(unittest.TestCase):
    def test_request_sources_survive_revalidation_and_dump(self):
        for request, expected in (
            ({"prompt": "你好"}, "你好"),
            ({"template_id": "summarize", "variables": {"text": "你好"}}, "请总结以下内容：\n你好"),
            ({"messages": [{"role": "user", "content": "你好"}]}, "你好"),
        ):
            with self.subTest(request=request):
                body = ChatInput.model_validate(request)
                self.assertEqual(body.model_fields_set, set(request))
                self.assertEqual(body.model_dump(exclude_unset=True), request)
                for value in (body, body.model_dump()):
                    rebuilt = ChatInput.model_validate(value)
                    self.assertEqual(rebuilt.model_messages(), [{"role": "user", "content": expected}])


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))
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

        async def reply(received, model):
            self.assertEqual(model, "deepseek-flash")
            self.assertEqual(received, messages)
            try:
                yield LLMEvent(type="delta", text="小")
                yield LLMEvent(type="delta", text="明")
                yield LLMEvent(type="usage", usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
                yield LLMEvent(type="done", finish_reason="stop")
            finally:
                closed.append(True)

        with patch("main.stream_reply", reply):
            response = await self.client.post("/chat", json={"messages": messages})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: start", response.text)
        self.assertIn('data: {"model": "deepseek-flash"}', response.text)
        self.assertEqual(response.text.count("event: delta"), 2)
        self.assertIn("event: done", response.text)
        self.assertEqual(closed, [True])
        self.assertIn('data: {"reason": "stop"}', response.text)
        self.assertIn('event: usage\ndata: {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}', response.text)
        self.assertLess(response.text.index("event: usage"), response.text.index("event: done"))

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
        async def reply(messages, model):
            yield LLMEvent(type="delta", text="部分内容")
            raise RuntimeError("private upstream details")

        with patch("main.stream_reply", reply):
            response = await self.client.post(
                "/chat", json={"messages": [{"role": "user", "content": "你好"}]}
            )
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: done", response.text)
        self.assertNotIn("private upstream details", response.text)

    async def test_explicit_model_forwarded(self):
        async def reply(messages, model):
            self.assertEqual(model, "deepseek-flash")
            yield LLMEvent(type="delta", text="你好")
            yield LLMEvent(type="done", finish_reason="stop")

        with patch("main.stream_reply", reply):
            response = await self.client.post("/chat", json={
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "你好"}],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('data: {"model": "deepseek-flash"}', response.text)
        self.assertIn("event: done", response.text)

    async def test_output_anomaly_is_sanitized_and_has_no_done(self):
        async def reply(messages, model):
            yield LLMEvent(type="delta", text="你好")
            raise OutputAnomalyError("检测到异常输出，诊断编号：test123")

        with patch("main.stream_reply", reply):
            response = await self.client.post("/chat", json={
                "messages": [{"role": "user", "content": "你好"}],
            })
        self.assertIn("event: error", response.text)
        payload = json.loads(response.text.split("event: error\ndata: ")[1])
        self.assertEqual(payload["code"], "UPSTREAM_BAD_RESPONSE")
        self.assertEqual(payload["request_id"], response.headers["X-Request-ID"])
        self.assertNotIn("test123", response.text)
        self.assertNotIn("event: done", response.text)

    async def test_unsupported_model_rejected_before_generation(self):
        for model in ("gpt-unknown", "", "deepseek-flash\n", None, 123):
            with self.subTest(model=model), patch("main.stream_reply") as reply:
                response = await self.client.post("/chat", json={
                    "model": model,
                    "messages": [{"role": "user", "content": "你好"}],
                })
                self.assertEqual(response.status_code, 422)
                error = response.json()["error"]
                self.assertEqual(error["code"], "INVALID_REQUEST")
                self.assertEqual(error["request_id"], response.headers["X-Request-ID"])
                self.assertIs(error["retryable"], False)
                reply.assert_not_called()

    async def test_legacy_prompt_normalized(self):
        for extra in ({}, {"model": "deepseek-flash"}):
            async def reply(messages, model):
                self.assertEqual(messages, [{"role": "user", "content": "你好"}])
                self.assertEqual(model, "deepseek-flash")
                yield LLMEvent(type="delta", text="你好！")
                yield LLMEvent(type="done", finish_reason="stop")

            with self.subTest(extra=extra), patch("main.stream_reply", reply):
                response = await self.client.post("/chat", json={"prompt": "你好", **extra})
            self.assertEqual(response.status_code, 200)
            self.assertIn('data: {"model": "deepseek-flash"}', response.text)
            self.assertIn("event: done", response.text)

    async def test_invalid_or_ambiguous_legacy_input(self):
        for body in (
            {}, {"prompt": None}, {"prompt": ""}, {"prompt": " "},
            {"prompt": "a" * 2001}, {"prompt": 123},
            {"prompt": "你好", "messages": []},
            {"prompt": "你好", "messages": [{"role": "user", "content": "另一问题"}]},
            {"prompt": "你好", "model": "gpt-unknown"},
        ):
            with self.subTest(body=body), patch("main.stream_reply") as reply:
                response = await self.client.post("/chat", json=body)
                self.assertEqual(response.status_code, 422)
                reply.assert_not_called()

    async def test_finish_reason_is_forwarded(self):
        async def reply(messages, model):
            yield LLMEvent(type="done", finish_reason="length")

        with patch("main.stream_reply", reply):
            response = await self.client.post("/chat", json={"prompt": "你好"})
        self.assertIn('data: {"reason": "length"}', response.text)
        self.assertNotIn("event: error", response.text)

    async def test_exhausted_stream_emits_error_instead_of_done(self):
        async def reply(messages, model):
            yield LLMEvent(type="delta", text="部分内容")

        with patch("main.stream_reply", reply):
            response = await self.client.post("/chat", json={"prompt": "你好"})
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.text.split("event: error\ndata: ")[1])
        self.assertEqual(payload["code"], "UPSTREAM_BAD_RESPONSE")
        self.assertNotIn("event: done", response.text)
