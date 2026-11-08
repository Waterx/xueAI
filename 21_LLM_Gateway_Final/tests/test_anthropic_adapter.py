"""使用真实 Anthropic SDK 和模拟 HTTP 传输，不调用真实 API。"""

from rate_limiter import RateLimiter

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic

from adapters.anthropic_adapter import AnthropicAdapter
from adapters.base import BaseLLMAdapter
from main import app
from model import stream_reply
from output_monitor import OutputAnomalyError


def sse(event, **payload):
    return f"event: {event}\ndata: {json.dumps(dict(type=event, **payload))}\n\n".encode()


class AnthropicWire(httpx.AsyncByteStream):
    def __init__(self, texts=("你", "好"), *, reason="end_turn", fail=False,
                 block=False, input_tokens=8, output_tokens=2, truncate=False, error_type=None, initial_usage=None, final_usage=None):
        self.initial_usage = initial_usage
        self.final_usage = final_usage
        self.texts = texts
        self.reason = reason
        self.fail = fail
        self.block = block
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.truncate = truncate
        self.error_type = error_type
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        message = dict(id="msg_test", type="message", role="assistant",
                       model="deepseek-flash", content=[], stop_reason=None,
                       stop_sequence=None)
        message["usage"] = self.initial_usage if self.initial_usage is not None else dict(
            input_tokens=self.input_tokens, output_tokens=0,
            cache_read_input_tokens=0, cache_creation_input_tokens=0)
        yield sse("message_start", message=message)
        yield sse("content_block_start", index=0,
                  content_block=dict(type="text", text=""))
        for text in self.texts:
            yield sse("content_block_delta", index=0,
                      delta=dict(type="text_delta", text=text))
        if self.fail:
            raise httpx.ReadError("simulated upstream failure")
        if self.error_type:
            yield sse("error", error={"type": self.error_type, "message": "private details"})
            return
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()
        yield sse("content_block_stop", index=0)
        if self.truncate:
            return
        payload = dict(delta=dict(stop_reason=self.reason, stop_sequence=None))
        payload["usage"] = self.final_usage if self.final_usage is not None else dict(output_tokens=self.output_tokens)
        yield sse("message_delta", **payload)
        yield sse("message_stop")

    async def aclose(self):
        self.closed = True


class AnthropicAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))

    async def exercise(self, wire, *, source=stream_reply, mode="normal", http=False):
        messages = [{"role": "user", "content": "你好"}]

        def handle(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/anthropic/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "test")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual(body["messages"], messages)
            self.assertEqual(body["max_tokens"], 1024)
            self.assertTrue(body["stream"])
            self.assertEqual(body["system"], "你是一位友好的助手，请用中文简洁回答。")
            self.assertEqual(body["thinking"], {"type": "disabled"})
            return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream"})

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        client = AsyncAnthropic(api_key="test", base_url="https://api.deepseek.com/anthropic",
                                http_client=http_client, max_retries=0)
        received = []
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(
            "adapters.anthropic_adapter.AsyncAnthropic", return_value=client
        ) as factory, patch("output_monitor.logger.error") as anomaly_log:
            events = source(messages, "deepseek-flash")

            async def consume():
                async for event in events:
                    received.append(event)

            try:
                if http:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://test") as browser:
                        response = await browser.post("/chat", json={"messages": messages})
                    self.assertEqual(response.status_code, 200)
                elif mode == "cancel":
                    task = asyncio.create_task(consume())
                    try:
                        await asyncio.wait_for(wire.waiting.wait(), timeout=2)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                elif mode == "close":
                    received.append(await anext(events))
                    await events.aclose()
                elif mode == "failure":
                    with self.assertRaises(httpx.ReadError):
                        await consume()
                elif mode == "anomaly":
                    with self.assertRaises(OutputAnomalyError):
                        await consume()
                    anomaly_log.assert_called_once()
                    self.assertEqual([event.text for event in received], ["你好<|im_"])
                elif mode == "invalid_stop":
                    with self.assertRaisesRegex(RuntimeError, "模型未正常完成"):
                        await consume()
                else:
                    await consume()

                self.assertTrue(wire.closed)
                self.assertTrue(http_client.is_closed)
                self.assertTrue(client.is_closed())
                factory.assert_called_once_with(api_key="test",
                                                base_url="https://api.deepseek.com/anthropic",
                                                timeout=60.0, max_retries=0)
                if http:
                    return response.text
                if mode != "normal":
                    self.assertNotIn("done", [event.type for event in received])
                return received
            finally:
                await events.aclose()
                await client.close()

    async def test_text_stream_and_normal_completion(self):
        self.assertIsInstance(AnthropicAdapter(), BaseLLMAdapter)
        for source in (AnthropicAdapter().stream, stream_reply):
            with self.subTest(source=source.__qualname__):
                events = await self.exercise(AnthropicWire(), source=source)
                self.assertEqual([e.type for e in events], ["delta", "delta", "usage", "done"])
                self.assertEqual([e.text for e in events[:2]], ["你", "好"])
                self.assertEqual(events[2].usage, dict(input_tokens=8, output_tokens=2, cache_read_input_tokens=0,
                                                          cache_creation_input_tokens=0, prompt_tokens=8, completion_tokens=2, total_tokens=10))
                self.assertEqual(events[-1].finish_reason, "stop")

    async def test_stop_reasons(self):
        for reason, expected in (("end_turn", "stop"), ("stop_sequence", "stop"),
                                 ("max_tokens", "length")):
            with self.subTest(reason=reason):
                events = await self.exercise(AnthropicWire(reason=reason))
                self.assertEqual(events[-1].finish_reason, expected)

    async def test_zero_usage_is_preserved(self):
        events = await self.exercise(AnthropicWire(input_tokens=0, output_tokens=0))
        self.assertEqual(events[-2].usage, dict(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                                             cache_creation_input_tokens=0, prompt_tokens=0, completion_tokens=0, total_tokens=0))

    async def test_unexpected_stop_does_not_report_success(self):
        for reason in (None, "tool_use", "unexpected"):
            with self.subTest(reason=reason):
                await self.exercise(AnthropicWire(reason=reason), mode="invalid_stop")

    async def test_truncated_stream_does_not_report_success(self):
        await self.exercise(AnthropicWire(truncate=True), mode="invalid_stop")

    async def test_resources_close_on_failure_cancel_close_and_anomaly(self):
        cases = (("failure", dict(fail=True)), ("cancel", dict(block=True)),
                 ("close", dict(block=True)),
                 ("anomaly", dict(texts=["你好<|im_", "end|>"])))
        for source in (AnthropicAdapter().stream, stream_reply):
            for mode, options in cases:
                with self.subTest(source=source.__qualname__, mode=mode):
                    await self.exercise(AnthropicWire(**options), source=source, mode=mode)

    async def test_missing_api_key(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "adapters.anthropic_adapter.AsyncAnthropic"
        ) as factory:
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                async for _ in AnthropicAdapter().stream([], "deepseek-flash"):
                    self.fail("未配置密钥时不应产生事件")
            factory.assert_not_called()

    async def test_chat_routes_through_anthropic_and_preserves_sse(self):
        text = await self.exercise(AnthropicWire(), http=True)
        self.assertEqual([line for line in text.splitlines() if line.startswith("event:")],
                         ["event: start", "event: delta", "event: delta", "event: usage", "event: done"])
        self.assertIn('data: {"reason": "stop"}', text)

    async def test_chat_reports_unexpected_stop_as_error(self):
        text = await self.exercise(AnthropicWire(reason="tool_use"), http=True)
        self.assertIn("event: start", text)
        self.assertIn("event: error", text)
        self.assertNotIn("event: done", text)

    async def test_chat_classifies_provider_error_inside_http_200(self):
        text = await self.exercise(AnthropicWire(error_type="rate_limit_error"), http=True)
        payload = json.loads(text.split("event: error\ndata: ")[1])
        self.assertEqual(payload["code"], "UPSTREAM_RATE_LIMITED")
        self.assertIs(payload["retryable"], True)
        self.assertNotIn("private details", text)
        self.assertNotIn("event: done", text)
