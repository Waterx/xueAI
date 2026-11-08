"""模板边界与 HTTP → 模型 → 真实 SDK 的离线回归。"""

import json
import os
import unittest
import tempfile
from pathlib import Path
import prompt_manager
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic
from jsonschema import validate
from openai import AsyncOpenAI

from main import app
from prompt_manager import render_prompt
from test_anthropic_adapter import AnthropicWire
from test_complete import message_payload, response_payload
from test_json_schema import FORMAT
from test_responses_adapter import ResponsesWire


def setUpModule():
    global temporary_db, db_patch
    temporary_db = tempfile.TemporaryDirectory()
    db_patch = patch.object(prompt_manager, "DB_PATH", Path(temporary_db.name) / "prompts.db")
    db_patch.start()


def tearDownModule():
    db_patch.stop()
    temporary_db.cleanup()


class PromptManagerTests(unittest.TestCase):
    def test_templates_and_extra_variables(self):
        self.assertEqual(render_prompt("translate", {"text": "hello", "language": "日语"}),
                         "请把 hello 翻译成日语")
        self.assertEqual(render_prompt("summarize", {"text": "内容", "unused": "忽略"}),
                         "请总结以下内容：\n内容")

    def test_unknown_and_missing(self):
        for template, variables, error in (
            ("missing", {}, "模板 ID 不存在"),
            ("translate", {"text": "hello"}, "language"),
            ("summarize", {}, "text"),
            ("summarize", {"text": 123}, "字符串"),
        ):
            with self.subTest(template=template, variables=variables):
                with self.assertRaisesRegex(ValueError, error):
                    render_prompt(template, variables)

    def test_values_are_literal_and_not_recursive(self):
        text = r"{{language}} {{other}} {{7 * 7}} __import__('os') \1 \g<1>"
        self.assertEqual(render_prompt("translate", {"text": text, "language": "日语"}),
                         f"请把 {text} 翻译成日语")


class PromptHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_requests_fail_before_model_in_both_modes(self):
        invalid = [
            ({"template_id": "unknown"}, "模板 ID 不存在"),
            ({"template_id": "translate", "variables": {"text": "hello"}}, "language"),
            ({"prompt": "hello", "template_id": "translate"}, "其中一种"),
            ({}, "其中一种"),
            ({"variables": {"text": "hello"}}, "其中一种"),
            ({"template_id": "summarize", "messages": []}, "其中一种"),
            ({"template_id": ""}, None),
            ({"template_id": "summarize", "variables": {"text": 123}}, None),
            ({"template_id": "summarize", "variables": None}, None),
            ({"template_id": "summarize", "variables": {"text": "a" * 2000}}, "2000"),
            ({"prompt": " "}, None),
            ({"prompt": "a" * 2001}, None),
        ]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("main.complete_reply") as complete, patch("main.stream_reply") as stream:
                for streaming in (False, True):
                    for body, error in invalid:
                        with self.subTest(streaming=streaming, body=body):
                            response = await client.post("/chat", json={**body, "stream": streaming})
                            self.assertEqual(response.status_code, 422)
                            if error:
                                self.assertIn(error, response.text)
                complete.assert_not_called()
                stream.assert_not_called()

    async def test_both_models_all_formats_and_input_modes(self):
        # 2 模型 × 2 流式模式 × 3 输出格式 × 2 输入方式。
        for pro in (False, True):
            for streaming in (False, True):
                for fmt in (None, {"type": "json_object"}, FORMAT):
                    for template in (False, True):
                        with self.subTest(pro=pro, stream=streaming, fmt=fmt, template=template):
                            await self.exercise(pro, streaming, fmt, template)

    async def exercise(self, pro, streaming, fmt, template):
        model = "deepseek-v4-pro" if pro else "deepseek-flash"
        text = "{{language}} 张三今年30岁。"
        prompt = f"请把 {text} 翻译成日语"
        content = '{"name":"张三","age":30}' if fmt else "你好"
        wire = (ResponsesWire(texts=[content]) if pro else AnthropicWire(texts=[content])) if streaming else None
        payload = response_payload() if pro else message_payload()
        if pro:
            payload["output"][0]["content"][0]["text"] = content
        else:
            payload["content"] = [{"type": "text", "text": content}]

        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(body["input" if pro else "messages"], [{"role": "user", "content": prompt}])
            self.assertEqual(body["stream"], streaming)
            if pro and fmt:
                self.assertEqual(body["text"]["format"], fmt)
            if streaming:
                return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=payload)

        sdk = AsyncOpenAI if pro else AsyncAnthropic
        module = "adapters.responses_adapter" if pro else "adapters.anthropic_adapter"
        client = sdk(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(module + "." + sdk.__name__, return_value=client):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
                    body = {"model": model, "stream": streaming}
                    body.update({"template_id": "translate", "variables": {"text": text, "language": "日语"}}
                                if template else {"prompt": prompt})
                    if fmt:
                        body["response_format"] = fmt
                    response = await browser.post("/chat", json=body)
            self.assertEqual(response.status_code, 200)
            if streaming:
                events = [(frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:]))
                          for frame in response.text.strip().split("\n\n")]
                self.assertEqual([event for event, _ in events], ["start", "delta", "usage", "done"])
                result = "".join(data["text"] for event, data in events if event == "delta")
                self.assertTrue(wire.closed)
            else:
                result = response.json()["content"]
            self.assertEqual(result, content)
            if fmt == FORMAT:
                validate(json.loads(result), FORMAT["schema"])
            self.assertTrue(client.is_closed())
        finally:
            await client.close()
