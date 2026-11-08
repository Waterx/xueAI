"""最终关：真实 SDK + 可控 HTTP 验证缓存口径、最终累计值和 SQLite。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic
from anthropic.types import Usage

from adapters.anthropic_adapter import normalize_usage
from main import app
from metrics import MetricsStore
from rate_limiter import RateLimiter
from test_anthropic_adapter import AnthropicWire
from test_complete import message_payload


class TokenUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_disjoint_categories_and_nested_creation_not_double_counted(self):
        raw = dict(input_tokens=10, output_tokens=2, cache_read_input_tokens=30,
                   cache_creation_input_tokens=5,
                   cache_creation={"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 0})
        result = normalize_usage(Usage.model_validate(raw))
        self.assertEqual(result, dict(raw, prompt_tokens=45, completion_tokens=2, total_tokens=47))

    def test_unknown_categories_and_output_remain_unknown(self):
        self.assertIsNone(normalize_usage(None))
        for missing in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
            for explicit_null in (False, True):
                raw = dict(input_tokens=10, output_tokens=2, cache_read_input_tokens=30,
                           cache_creation_input_tokens=5)
                raw.pop(missing)
                if explicit_null:
                    raw[missing] = None
                result = normalize_usage(Usage.model_construct(**raw))
                self.assertIsNone(result["total_tokens"])
                if missing != "output_tokens":
                    self.assertIsNone(result["prompt_tokens"])
                self.assertEqual(result.get(missing), None)
                if not explicit_null:
                    self.assertNotIn(missing, result)

    def test_explicit_zero_is_known(self):
        result = normalize_usage(Usage.model_validate(dict(
            input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)))
        self.assertEqual(result["total_tokens"], 0)
        self.assertEqual(result["prompt_tokens"], 0)

    async def test_http_sse_final_usage_metrics_and_resources(self):
        final = dict(input_tokens=10, output_tokens=2, cache_read_input_tokens=30,
                     cache_creation_input_tokens=5)
        expected = dict(final, prompt_tokens=45, completion_tokens=2, total_tokens=47)
        for streaming in (False, True):
            with self.subTest(stream=streaming), tempfile.TemporaryDirectory() as tmp:
                store = MetricsStore(Path(tmp) / "metrics.db")
                wire = AnthropicWire(initial_usage=dict(input_tokens=100, output_tokens=0,
                    cache_read_input_tokens=200, cache_creation_input_tokens=50), final_usage=final)
                def handle(request):
                    if streaming:
                        return httpx.Response(200, stream=wire, headers={"content-type": "text/event-stream"})
                    payload = message_payload()
                    payload["usage"] = final
                    return httpx.Response(200, json=payload)
                transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
                sdk = AsyncAnthropic(api_key="test", http_client=transport)
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(
                    "adapters.anthropic_adapter.AsyncAnthropic", return_value=sdk
                ), patch("main.metrics_store", store), patch("main.rate_limiter", RateLimiter()):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        response = await client.post("/chat", json={"prompt": "hello", "stream": streaming})
                        self.assertEqual(response.status_code, 200)
                        if streaming:
                            frames = [part.split("\ndata: ") for part in response.text.strip().split("\n\n")]
                            self.assertEqual(frames[-1][0], "event: done")
                            usages = [json.loads(data) for event, data in frames if event == "event: usage"]
                            self.assertEqual(usages, [expected])
                        else:
                            self.assertEqual(response.json()["usage"], expected)
                        rows = (await client.get("/metrics/requests")).json()
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["usage"], expected)
                        self.assertEqual(rows[0]["input_tokens"], 45)
                        self.assertEqual(rows[0]["total_tokens"], 47)
                        self.assertEqual(rows[0]["request_id"], response.headers["X-Request-ID"])
                        if streaming:
                            self.assertGreaterEqual(rows[0]["ttft_ms"], 0)
                            self.assertTrue(wire.closed)
                        self.assertEqual((await client.get("/metrics")).json()["deepseek-flash"]["input_tokens"], 45)
                self.assertTrue(sdk.is_closed())
                self.assertTrue(transport.is_closed)
                self.assertEqual(MetricsStore(store.db_path).recent(1, None)[0]["usage"], expected)
