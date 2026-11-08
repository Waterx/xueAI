"""经 HTTP 层、真实 SDK 与 MockTransport 验证 Schema 成功和失败。"""

from rate_limiter import RateLimiter
import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from main import app
from test_complete import message_payload, response_payload

FORMAT = {
    "type": "json_schema", "name": "person",
    "schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"], "additionalProperties": False,
    },
}


class JSONSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))

    async def exercise(self, pro, content, response_format=None):
        fmt = copy.deepcopy(FORMAT if response_format is None else response_format)
        model = "deepseek-v4-pro" if pro else "deepseek-flash"
        payload = response_payload() if pro else message_payload()
        if pro:
            payload["output"][0]["content"][0]["text"] = content
        else:
            payload["content"] = [{"type": "text", "text": content}]

        async def handle(request):
            body = json.loads(request.content)
            self.assertFalse(body["stream"])
            for field in ("tools", "tool_choice", "output_config", "response_format"):
                self.assertNotIn(field, body)
            if pro:
                self.assertEqual(body["text"], {"format": fmt})
            else:
                self.assertIn(json.dumps(fmt["schema"], ensure_ascii=False), body["system"])
                self.assertIn("Markdown", body["system"])
            return httpx.Response(200, json=payload)

        sdk = AsyncOpenAI if pro else AsyncAnthropic
        module = "adapters.responses_adapter" if pro else "adapters.anthropic_adapter"
        client = sdk(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(module + "." + sdk.__name__, return_value=client):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
                return await browser.post("/chat", json={
                    "model": model, "prompt": "提取信息：张三今年30岁。", "stream": False,
                    "response_format": fmt,
                })

    async def test_success_both_protocols(self):
        for pro in (False, True):
            content = ' {"name":"张三","age":30} '
            response = await self.exercise(pro, content)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["content"], content)
            self.assertEqual(response.json()["finish_reason"], "stop")

    async def test_invalid_outputs_are_502_both_protocols(self):
        for pro in (False, True):
            for content in ('{"name":"张三"}', '{"name":"张三","age":"三十"}',
                            '{"name":"张三","age":30,"extra":1}', '{"name":',
                            '```json\n{}\n```', '[]', '{"name":"张三","age":NaN}'):
                with self.subTest(pro=pro, content=content):
                    response = await self.exercise(pro, content)
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(response.json()["error"]["code"], "STRUCTURED_OUTPUT_INVALID")
                    self.assertNotIn("content", response.json())

    async def test_invalid_requests_are_422_before_call(self):
        invalid = [
            {"type": "json_schema", "name": "person"},
            {"type": "json_schema", "schema": {}},
            {**FORMAT, "schema": {"type": "unknown"}},
            {**FORMAT, "schema": {"required": "age"}},
            {**FORMAT, "schema": {"properties": {"age": {"type": "unknown"}}}},
            {**FORMAT, "schema": None}, {**FORMAT, "schema": []},
            {**FORMAT, "schema": 0}, {**FORMAT, "schema": "false"},
            {**FORMAT, "name": ""}, {**FORMAT, "extra": True},
        ]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("main.complete_reply") as complete, patch("main.stream_reply") as stream:
                for fmt in invalid:
                    response = await client.post("/chat", json={"prompt": "JSON", "stream": False, "response_format": fmt})
                    self.assertEqual(response.status_code, 422)
                complete.assert_not_called()
                stream.assert_not_called()

    async def test_schema_controls_root_and_constraints(self):
        for pro in (False, True):
            fmt = {**FORMAT, "schema": {"type": "array", "items": {"type": "integer"}, "minItems": 1}}
            self.assertEqual((await self.exercise(pro, '[30]', fmt)).status_code, 200)
            self.assertEqual((await self.exercise(pro, '[]', fmt)).status_code, 502)
            self.assertEqual((await self.exercise(pro, '{}', {**FORMAT, "schema": False})).status_code, 502)
