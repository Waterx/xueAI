"""JSON Object 协议与本地验证：真实 SDK + MockTransport。"""
import json
import os
import unittest
from unittest.mock import patch
import httpx
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
from main import app
from model import complete_reply, StructuredOutputError
from test_complete import response_payload, message_payload

FORMAT = {"type": "json_object"}

class JSONObjectTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, pro, content, truncated=False, structured=True):
        model = "deepseek-v4-pro" if pro else "deepseek-flash"
        payload = response_payload() if pro else message_payload()
        if pro:
            payload["output"][0]["content"][0]["text"] = content
            if truncated:
                payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        else:
            payload["content"] = [{"type": "text", "text": content}]
            if truncated:
                payload["stop_reason"] = "max_tokens"
        messages = [{"role": "system", "content": "保留原有规则"}, {"role": "user", "content": "返回 JSON"}]
        async def handle(request):
            body = json.loads(request.content)
            self.assertFalse(body["stream"])
            instruction = body["instructions" if pro else "system"]
            self.assertIn("保留原有规则", instruction)
            self.assertEqual(body["input" if pro else "messages"], messages[1:])
            if structured:
                self.assertIn("JSON", instruction)
                self.assertIn("Markdown", instruction)
                if pro:
                    self.assertEqual(body["text"], {"format": FORMAT})
            else:
                self.assertEqual(instruction, "保留原有规则")
                self.assertNotIn("text", body)
            self.assertNotIn("response_format", body)
            return httpx.Response(200, json=payload)
        sdk = AsyncOpenAI if pro else AsyncAnthropic
        module = "adapters.responses_adapter" if pro else "adapters.anthropic_adapter"
        client = sdk(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(module + "." + sdk.__name__, return_value=client):
            return await complete_reply(messages, model, FORMAT if structured else None)

    async def test_valid_objects_keep_string_and_fields(self):
        for pro in (False, True):
            text = ' {"中文": [1, true, null], "nested": {}} '
            result = await self.exercise(pro, text)
            self.assertEqual(result.content, text)
            self.assertIsInstance(json.loads(result.content), dict)
            self.assertEqual(set(result.model_dump()), {"model", "content", "finish_reason", "usage"})

    async def test_invalid_json_and_non_objects(self):
        for pro in (False, True):
            for text in ('{"a":', '```json\n{}\n```', '{} trailing', '[]', 'null', '42', '{"x": NaN}', '{"x": Infinity}'):
                with self.subTest(pro=pro, text=text), self.assertRaises(StructuredOutputError):
                    await self.exercise(pro, text)

    async def test_truncation_even_when_parseable(self):
        for pro in (False, True):
            for text in ('{}', '{"a":'):
                with self.subTest(pro=pro, text=text), self.assertRaises((StructuredOutputError, RuntimeError)):
                    await self.exercise(pro, text, truncated=True)

    async def test_plain_text_unchanged(self):
        for pro in (False, True):
            result = await self.exercise(pro, "普通文字", structured=False)
            self.assertEqual(result.content, "普通文字")
        result = await self.exercise(False, "截断文字", truncated=True, structured=False)
        self.assertEqual(result.finish_reason, "length")

    async def test_http_validation_before_model_call(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("main.complete_reply") as complete, patch("main.stream_reply") as stream:
                for options in ({"response_format": FORMAT}, {"response_format": FORMAT, "stream": True},
                                *({"response_format": value, "stream": False} for value in
                                  ({}, {"type": "text"}, {"type": "json_schema"}, {"type": "json_object", "extra": 1}, "json_object"))):
                    response = await client.post("/chat", json={"prompt": "JSON", **options})
                    self.assertEqual(response.status_code, 422)
                complete.assert_not_called()
                stream.assert_not_called()

    async def test_http_success_and_validation_failure(self):
        from schemas import LLMResult
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for model in ("deepseek-flash", "deepseek-v4-pro"):
                result = LLMResult(model=model, content='{"ok":true}', finish_reason="stop")
                with patch("main.complete_reply", return_value=result) as complete:
                    response = await client.post("/chat", json={"model": model, "prompt": "JSON", "stream": False, "response_format": FORMAT})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), result.model_dump())
                    self.assertEqual(complete.call_args.kwargs["response_format"], FORMAT)
                with patch("main.complete_reply", side_effect=StructuredOutputError("结构化输出不是合法 JSON")):
                    response = await client.post("/chat", json={"model": model, "prompt": "JSON", "stream": False, "response_format": FORMAT})
                    self.assertEqual(response.status_code, 502)
                    self.assertNotIn("content", response.json())
