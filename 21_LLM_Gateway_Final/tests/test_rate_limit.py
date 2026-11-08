import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from errors import RateLimitError, UpstreamResponseError, normalize_error
from main import app
from metrics import MetricsStore
from rate_limiter import RateLimiter
from schemas import LLMEvent, LLMResult


class WindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_t1_t2_t3_independent_default_limits(self):
        limiter = RateLimiter()
        for model, count in [('deepseek-flash', 5), ('deepseek-v4-pro', 3)]:
            for _ in range(count):
                await limiter.acquire(model)
            with self.assertRaises(RateLimitError) as caught:
                await limiter.acquire(model)
            self.assertFalse(normalize_error(caught.exception).retryable)

    async def test_t4_t7_sliding_expiry_and_ceil(self):
        now = [0.0]
        limiter = RateLimiter({'m': 2}, clock=lambda: now[0])
        await limiter.acquire('m')
        now[0] = 12
        await limiter.acquire('m')
        now[0] = 47.7
        for _ in range(2):
            with self.assertRaises(RateLimitError) as caught:
                await limiter.acquire('m')
            self.assertEqual(caught.exception.retry_after, 13)
        now[0] = 60
        await limiter.acquire('m')
        with self.assertRaises(RateLimitError) as caught:
            await limiter.acquire('m')
        self.assertEqual(caught.exception.retry_after, 12)
        now[0] = 120
        await limiter.acquire('m')
        await limiter.acquire('m')

    async def test_t5_concurrent_last_slot(self):
        limiter = RateLimiter({'m': 3})
        await limiter.acquire('m')
        await limiter.acquire('m')
        results = await asyncio.gather(*(limiter.acquire('m') for _ in range(30)), return_exceptions=True)
        self.assertEqual(results.count(None), 1)
        self.assertEqual(sum(isinstance(r, RateLimitError) for r in results), 29)

    def test_invalid_configuration(self):
        for limits in ({}, {'m': 0}, {'m': -1}, {'m': True}, {'m': 1.5}):
            with self.assertRaises(ValueError):
                RateLimiter(limits)
        for window in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                RateLimiter(window_seconds=window)


class RateLimitHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = MetricsStore(Path(directory) / 'metrics.db')
        self.enterContext(patch('main.metrics_store', self.store))
        self.now = 0.0
        self.limiter = RateLimiter({'deepseek-flash': 1, 'deepseek-v4-pro': 1}, clock=lambda: self.now)
        self.enterContext(patch('main.rate_limiter', self.limiter))
        self.wait = self.enterContext(patch('retry.wait_before_retry', new_callable=AsyncMock))
        self.complete = self.enterContext(patch('main.complete_reply', new_callable=AsyncMock))
        self.complete.return_value = LLMResult(model='deepseek-flash', content='ok', finish_reason='stop')
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test'))

    async def post(self, streaming=False, model='deepseek-flash', **extra):
        return await self.client.post('/chat', json={'prompt': 'hi', 'stream': streaming, 'model': model, **extra})

    async def test_t6_t7_t8_t12_rejection_and_metrics(self):
        self.assertEqual((await self.post()).status_code, 200)
        self.now = 47.7
        stream = self.enterContext(patch('main.stream_reply'))
        for streaming in (False, True):
            response = await self.post(streaming)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['Retry-After'], '13')
            self.assertEqual(response.json()['error'], {
                'code': 'RATE_LIMIT_EXCEEDED', 'message': '当前模型请求过于频繁，请稍后重试。',
                'retryable': False, 'request_id': response.headers['X-Request-ID']})
            self.assertNotIn('event: start', response.text)
            self.assertIn('application/json', response.headers['content-type'])
        stream.assert_not_called()
        self.complete.assert_awaited_once()
        self.wait.assert_not_awaited()
        self.assertEqual(len(self.store.recent(10, None)), 1)
        self.assertEqual((await self.post(model='deepseek-v4-pro')).status_code, 200)
        self.now = 60
        self.assertEqual((await self.post()).status_code, 200)

    async def test_t9_upstream_429_and_failed_request_consumes_quota(self):
        self.complete.side_effect = UpstreamResponseError('private', 'rate_limit_error')
        response = await self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'UPSTREAM_RATE_LIMITED')
        self.assertNotIn('Retry-After', response.headers)
        self.assertEqual(self.complete.await_count, 4)
        self.assertEqual((await self.post()).status_code, 429)
        self.assertEqual(self.complete.await_count, 4)
        row, = self.store.recent(10, None)
        self.assertEqual(row['status'], 'error')

    async def test_t10_retry_counts_once_for_both_modes(self):
        for streaming, model in [(False, 'deepseek-flash'), (True, 'deepseek-v4-pro')]:
            self.complete.side_effect = [UpstreamResponseError('', 'server_error'),
                                         UpstreamResponseError('', 'server_error'), self.complete.return_value]
            async def source(*args, **kwargs):
                await self.complete()
                yield LLMEvent(type='delta', text='ok')
                yield LLMEvent(type='done', finish_reason='stop')
            with patch('main.stream_reply', source):
                response = await self.post(streaming, model)
                self.assertEqual(response.status_code, 200)
                if streaming:
                    self.assertEqual(response.text.count('event: start'), 1)
                    self.assertIn('event: done', response.text)
                self.assertEqual((await self.post(streaming, model)).status_code, 429)
        self.assertEqual(self.complete.await_count, 6)
        self.assertEqual(len(self.store.recent(10, None)), 2)

    async def test_t11_invalid_requests_do_not_consume_quota(self):
        for body in ({}, {'prompt': ' '}, {'prompt': 'hi', 'model': 'unknown'}):
            self.assertEqual((await self.client.post('/chat', json=body)).status_code, 422)
        with patch('main.render_prompt', side_effect=ValueError('missing template')):
            response = await self.client.post('/chat', json={'template_id': 'missing'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.recent(10, None), [])
        self.assertEqual((await self.post()).status_code, 200)

    async def test_http_concurrency(self):
        responses = await asyncio.gather(*(self.post() for _ in range(20)))
        self.assertEqual(sum(r.status_code == 200 for r in responses), 1)
        self.assertEqual(sum(r.status_code == 429 for r in responses), 19)
        self.complete.assert_awaited_once()
        self.assertEqual(len(self.store.recent(30, None)), 1)
