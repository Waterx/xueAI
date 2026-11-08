"""真实 SDK + MockTransport 验证非流式协议、路由和失败行为，无需联网。"""

import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from adapters.base import BaseLLMAdapter
from adapters.anthropic_adapter import AnthropicAdapter
from adapters.responses_adapter import ResponsesAdapter
from adapters.registry import ADAPTERS
from main import app
from model import complete_reply
from schemas import LLMResult


MESSAGES = [{"role": "user", "content": "你好"}]
USAGE = dict(prompt_tokens=8, completion_tokens=2, total_tokens=10)


def response_payload():
    return dict(id="resp_test", object="response", created_at=0,
                model="deepseek-v4-pro", status="completed", error=None,
                incomplete_details=None, output=[dict(
                    id="msg_test", type="message", role="assistant", status="completed",
                    content=[dict(type="output_text", text="你好", annotations=[])])],
                usage=dict(input_tokens=8, output_tokens=2, total_tokens=10,
                           input_tokens_details=dict(cached_tokens=0),
                           output_tokens_details=dict(reasoning_tokens=0)))


def message_payload():
    return dict(id="msg_test", type="message", role="assistant", model="deepseek-flash",
                content=[dict(type="thinking", thinking="不输出推理", signature="test"),
                         dict(type="text", text="你"), dict(type="text", text="好")],
                stop_reason="end_turn", stop_sequence=None,
                usage=dict(input_tokens=8, output_tokens=2))


class CompleteTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, model, *, payload=None, messages=None, http=False,
                       status=200, error=None, expected_error=None):
        pro = model == "deepseek-v4-pro"
        module = "adapters.responses_adapter" if pro else "adapters.anthropic_adapter"
        sdk = AsyncOpenAI if pro else AsyncAnthropic
        base_url = "https://api.deepseek.com" + ("" if pro else "/anthropic")
        payload = payload if payload is not None else (response_payload() if pro else message_payload())
        messages = messages if messages is not None else copy.deepcopy(MESSAGES)
        original = copy.deepcopy(messages)

        async def handle(request):
            self.assertEqual(str(request.url), base_url + ("/responses" if pro else "/v1/messages"))
            body = json.loads(request.content)
            self.assertIs(body["stream"], False)
            self.assertEqual(body["model"], model)
            if pro:
                self.assertEqual(body["input"], [m for m in messages if m["role"] != "system"])
                systems = [m["content"] for m in messages if m["role"] == "system"]
                self.assertEqual(body["instructions"], "\n\n".join(systems) if systems
                                 else "你是一位友好的助手，请用中文简洁回答。")
                self.assertEqual(body["max_output_tokens"], 1024)
                self.assertEqual(body["reasoning"], {"effort": "none"})
            else:
                self.assertEqual(body["messages"], messages)
                self.assertEqual(body["max_tokens"], 1024)
                self.assertEqual(body["thinking"], {"type": "disabled"})
            if error == "cancel":
                raise asyncio.CancelledError()
            if error == "timeout":
                raise httpx.ReadTimeout("private upstream detail", request=request)
            if error == "connection":
                raise httpx.ConnectError("private upstream detail", request=request)
            return httpx.Response(status, json=payload)

        transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        client = sdk(api_key="test", base_url=base_url, http_client=transport, max_retries=0)
        try:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(
                module + "." + sdk.__name__, return_value=client
            ) as factory, patch("output_monitor.logger.error"):
                if http:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://test") as browser:
                        result = await browser.post("/chat", json=dict(
                            model=model, messages=messages, stream=False))
                elif expected_error:
                    with self.assertRaises(expected_error):
                        await complete_reply(messages, model)
                    result = None
                else:
                    result = await complete_reply(messages, model)
                factory.assert_called_once_with(api_key="test", base_url=base_url,
                                                timeout=60.0, max_retries=0)
                self.assertEqual(messages, original)
                self.assertTrue(client.is_closed())
                self.assertTrue(transport.is_closed)
                return result
        finally:
            await client.close()

    async def test_both_adapters_result_and_json(self):
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            for http in (False, True):
                with self.subTest(model=model, http=http):
                    result = await self.exercise(model, http=http)
                    expected = dict(model=model, content="你好", finish_reason="stop", usage=USAGE)
                    if http:
                        self.assertEqual(result.status_code, 200)
                        self.assertIn("application/json", result.headers["content-type"])
                        self.assertEqual(result.json(), expected)
                    else:
                        self.assertIsInstance(result, LLMResult)
                        self.assertEqual(result.model_dump(), expected)

    async def test_responses_system_and_history(self):
        await self.exercise("deepseek-v4-pro", messages=[
            dict(role="system", content="规则一"), dict(role="system", content="规则二"),
            *MESSAGES, dict(role="assistant", content="回答"), dict(role="user", content="追问")])

    async def test_anthropic_stop_reasons(self):
        for reason, expected in (("end_turn", "stop"), ("stop_sequence", "stop"),
                                 ("max_tokens", "length")):
            payload = message_payload()
            payload["stop_reason"] = reason
            with self.subTest(reason=reason):
                result = await self.exercise("deepseek-flash", payload=payload)
                self.assertEqual(result.finish_reason, expected)

    async def test_missing_and_zero_usage(self):
        for model, make in (("deepseek-flash", message_payload), ("deepseek-v4-pro", response_payload)):
            for usage in (None, dict(input_tokens=0, output_tokens=0, total_tokens=0)):
                payload = make()
                payload["usage"] = usage
                with self.subTest(model=model, usage=usage):
                    result = await self.exercise(model, payload=payload)
                    self.assertEqual(result.usage, None if usage is None else
                                     dict(prompt_tokens=0, completion_tokens=0, total_tokens=0))

    async def test_responses_rejects_abnormal_status_and_error(self):
        for changes in (dict(status="failed"), dict(status="incomplete"), dict(status=None),
                        dict(status="queued"), dict(error=dict(code="server_error", message="private")),
                        dict(incomplete_details=dict(reason="max_output_tokens"))):
            payload = response_payload()
            payload.update(changes)
            with self.subTest(changes=changes):
                await self.exercise("deepseek-v4-pro", payload=payload, expected_error=RuntimeError)
                result = await self.exercise("deepseek-v4-pro", payload=payload, http=True)
                self.assertEqual(result.status_code, 502)
                self.assertNotIn("content", result.json())

    async def test_anthropic_rejects_unexpected_stop(self):
        for reason in (None, "tool_use", "unexpected"):
            payload = message_payload()
            payload["stop_reason"] = reason
            with self.subTest(reason=reason):
                await self.exercise("deepseek-flash", payload=payload, expected_error=RuntimeError)
                result = await self.exercise("deepseek-flash", payload=payload, http=True)
                self.assertEqual(result.status_code, 502)

    async def test_http_failure_timeout_and_private_errors(self):
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            for error, status in (("timeout", 504), ("connection", 502), (None, 502)):
                with self.subTest(model=model, error=error):
                    result = await self.exercise(model, http=True, error=error, status=503,
                        payload={"error": {"type": "api_error", "message": "private upstream detail"}})
                    self.assertEqual(result.status_code, status)
                    self.assertEqual(set(result.json()), {"detail"})
                    self.assertNotIn("private", result.text)

    async def test_cancellation_propagates_and_closes_clients(self):
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            await self.exercise(model, error="cancel", expected_error=asyncio.CancelledError)

    async def test_output_anomaly_is_http_error(self):
        for model, make in (("deepseek-flash", message_payload), ("deepseek-v4-pro", response_payload)):
            payload = make()
            if model == "deepseek-flash":
                payload["content"] = [dict(type="text", text="你好<|im_end|>")]
            else:
                payload["output"][0]["content"][0]["text"] = "你好<|im_end|>"
            result = await self.exercise(model, payload=payload, http=True)
            self.assertEqual(result.status_code, 502)

    async def test_dynamic_registry_and_unknown_model(self):
        class DynamicAdapter(BaseLLMAdapter):
            async def complete(self, messages, model):
                self_test.assertEqual(messages, MESSAGES)
                return LLMResult(model=model, content="动态路由", finish_reason="stop")

            async def stream(self, messages, model):
                raise NotImplementedError
                yield

        self_test = self
        with patch.dict(ADAPTERS, {"test-model": DynamicAdapter}):
            result = await complete_reply(MESSAGES, "test-model")
        self.assertEqual(result.model, "test-model")
        self.assertEqual(result.content, "动态路由")
        with self.assertRaisesRegex(ValueError, "不支持的模型"):
            await complete_reply(MESSAGES, "unknown")

    async def test_missing_key_and_invalid_role(self):
        with patch.dict(os.environ, {}, clear=True):
            for adapter in (AnthropicAdapter(), ResponsesAdapter()):
                with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                    await adapter.complete(MESSAGES, "test")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}):
            with self.assertRaisesRegex(ValueError, "不支持的消息角色"):
                await ResponsesAdapter().complete([dict(role="tool", content="x")], "deepseek-v4-pro")

    async def test_nonstream_input_validation(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for body in (dict(model="unknown", prompt="你好", stream=False),
                         dict(messages=[], stream=False), dict(prompt=" ", stream=False)):
                result = await client.post("/chat", json=body)
                self.assertEqual(result.status_code, 422)

    async def test_legacy_prompt_with_nonstream(self):
        expected = LLMResult(model="deepseek-flash", content="你好", finish_reason="stop")
        with patch("main.complete_reply", return_value=expected) as complete:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.post("/chat", json=dict(prompt="你好", stream=False))
        self.assertEqual(result.json(), expected.model_dump())
        complete.assert_awaited_once_with(MESSAGES, model="deepseek-flash")


class AbstractCompleteTests(unittest.TestCase):
    def test_stream_only_subclass_is_abstract(self):
        class StreamOnly(BaseLLMAdapter):
            async def stream(self, messages, model):
                yield
        with self.assertRaises(TypeError):
            StreamOnly()
