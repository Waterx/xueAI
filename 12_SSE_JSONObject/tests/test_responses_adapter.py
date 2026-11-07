"""真实 Responses SDK + MockTransport；不访问网络。"""

import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from adapters.responses_adapter import ResponsesAdapter
from adapters.anthropic_adapter import AnthropicAdapter
from adapters.base import BaseLLMAdapter
from adapters.registry import ADAPTERS, get_adapter
from main import app
from model import stream_reply
from output_monitor import OutputAnomalyError


def sse(kind, **payload):
    return f"event: {kind}\ndata: {json.dumps(dict(type=kind, **payload))}\n\n".encode()


class ResponsesWire(httpx.AsyncByteStream):
    def __init__(self, *, terminal="response.completed", status="completed",
                 texts=("你", "好"), usage=True, fail=False, block=False,
                 error=None, incomplete_details=None):
        self.terminal, self.status, self.texts = terminal, status, texts
        self.usage, self.fail, self.block = usage, fail, block
        self.error, self.incomplete_details = error, incomplete_details
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield sse("response.created", response={"id": "resp_test"}, sequence_number=0)
        yield sse("response.reasoning_text.delta", delta="不应作为回答输出")
        for text in self.texts:
            yield sse("response.output_text.delta", delta=text,
                      item_id="msg_test", output_index=0, content_index=0, sequence_number=1)
        if self.fail:
            raise httpx.ReadError("simulated failure")
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.terminal is None:
            return
        if self.terminal == "error":
            yield sse("error", code="server_error", message="private details", param=None)
            return
        usage = dict(input_tokens=8, output_tokens=2, total_tokens=10,
                     input_tokens_details={"cached_tokens": 0},
                     output_tokens_details={"reasoning_tokens": 0}) if self.usage else None
        yield sse(self.terminal, sequence_number=2, response=dict(
            id="resp_test", object="response", created_at=0, status=self.status,
            model="deepseek-v4-pro", output=[], usage=usage,
            error=self.error, incomplete_details=self.incomplete_details))

    async def aclose(self):
        self.closed = True


class ResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, wire, *, source=stream_reply, mode="normal", http=False,
                       messages=None):
        messages = messages if messages is not None else [{"role": "user", "content": "你好"}]
        original = copy.deepcopy(messages)
        def handle(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/responses")
            self.assertEqual(request.headers["authorization"], "Bearer test")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "deepseek-v4-pro")
            self.assertTrue(body["stream"])
            self.assertEqual(body["input"], [m for m in messages if m["role"] != "system"])
            systems = [m["content"] for m in messages if m["role"] == "system"]
            self.assertEqual(body["instructions"], "\n\n".join(systems) if systems
                             else "你是一位友好的助手，请用中文简洁回答。")
            return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream"})
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        client = AsyncOpenAI(api_key="test", base_url="https://api.deepseek.com",
                             http_client=http_client, max_retries=0)
        received = []
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(
            "adapters.responses_adapter.AsyncOpenAI", return_value=client
        ) as factory, patch("output_monitor.logger.error"):
            events = source(messages, "deepseek-v4-pro")
            async def consume():
                async for event in events:
                    received.append(event)
            try:
                if http:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://test") as browser:
                        response = await browser.post("/chat", json={"model": "deepseek-v4-pro",
                                                                      "messages": messages})
                    self.assertEqual(response.status_code, 200)
                elif mode == "cancel":
                    task = asyncio.create_task(consume())
                    try:
                        await asyncio.wait_for(wire.waiting.wait(), 2)
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
                elif mode in ("invalid", "failure", "anomaly"):
                    error = {"invalid": RuntimeError, "failure": httpx.ReadError,
                             "anomaly": OutputAnomalyError}[mode]
                    with self.assertRaises(error):
                        await consume()
                else:
                    await consume()
                self.assertTrue(wire.closed)
                self.assertTrue(http_client.is_closed)
                self.assertTrue(client.is_closed())
                self.assertEqual(messages, original)
                factory.assert_called_once_with(api_key="test", base_url="https://api.deepseek.com",
                                                timeout=60.0, max_retries=0)
                if mode != "normal":
                    self.assertNotIn("done", [e.type for e in received])
                return response.text if http else received
            finally:
                await events.aclose()
                await client.close()

    async def test_text_usage_done_and_registry_route(self):
        self.assertIsInstance(ResponsesAdapter(), BaseLLMAdapter)
        self.assertIs(ADAPTERS["deepseek-flash"], AnthropicAdapter)
        self.assertIsInstance(get_adapter("deepseek-v4-pro"), ResponsesAdapter)
        self.assertIsNot(get_adapter("deepseek-v4-pro"), get_adapter("deepseek-v4-pro"))
        for source in (ResponsesAdapter().stream, stream_reply):
            with self.subTest(source=source.__qualname__):
                events = await self.exercise(ResponsesWire(), source=source)
                self.assertEqual([e.type for e in events], ["delta", "delta", "usage", "done"])
                self.assertEqual([e.text for e in events[:2]], ["你", "好"])
                self.assertEqual(events[2].usage, dict(prompt_tokens=8, completion_tokens=2, total_tokens=10))
                self.assertEqual(events[-1].finish_reason, "stop")

    async def test_system_and_history_conversion(self):
        await self.exercise(ResponsesWire(), messages=[
            {"role": "system", "content": "规则一"}, {"role": "system", "content": "规则二"},
            {"role": "user", "content": "问题一"}, {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "问题二"}])

    async def test_usage_absent(self):
        events = await self.exercise(ResponsesWire(usage=False))
        self.assertEqual([e.type for e in events], ["delta", "delta", "done"])

    async def test_abnormal_completion(self):
        cases = [dict(terminal=None), dict(terminal="response.failed", status="failed"),
                 dict(terminal="response.incomplete", status="incomplete"), dict(terminal="error"),
                 dict(status="failed"), dict(status="incomplete"), dict(status=None),
                 dict(error={"code": "server_error", "message": "private details"}),
                 dict(incomplete_details={"reason": "max_output_tokens"})]
        for options in cases:
            with self.subTest(options=options):
                await self.exercise(ResponsesWire(**options), mode="invalid")

    async def test_cleanup_on_failure_cancellation_close_and_anomaly(self):
        for source in (ResponsesAdapter().stream, stream_reply):
            for mode, options in [("failure", dict(fail=True)), ("cancel", dict(block=True)),
                                  ("close", dict(block=True)),
                                  ("anomaly", dict(texts=["你好<|im_", "end|>"]))]:
                with self.subTest(source=source.__qualname__, mode=mode):
                    await self.exercise(ResponsesWire(**options), source=source, mode=mode)

    async def test_http_pro_sse_and_failure(self):
        text = await self.exercise(ResponsesWire(), http=True)
        self.assertEqual([line for line in text.splitlines() if line.startswith("event:")],
                         ["event: start", "event: delta", "event: delta", "event: usage", "event: done"])
        self.assertIn('"model": "deepseek-v4-pro"', text)
        for terminal in ("response.failed", "response.incomplete", None):
            with self.subTest(terminal=terminal):
                text = await self.exercise(ResponsesWire(terminal=terminal, status="failed"), http=True)
                self.assertIn("event: error", text)
                self.assertNotIn("event: done", text)

    async def test_missing_key_and_unsupported_role(self):
        with patch.dict(os.environ, {}, clear=True), patch("adapters.responses_adapter.AsyncOpenAI") as factory:
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                await anext(ResponsesAdapter().stream([], "deepseek-v4-pro"))
            factory.assert_not_called()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}):
            with self.assertRaisesRegex(ValueError, "不支持的消息角色"):
                await anext(ResponsesAdapter().stream([{"role": "tool", "content": "x"}], "deepseek-v4-pro"))
