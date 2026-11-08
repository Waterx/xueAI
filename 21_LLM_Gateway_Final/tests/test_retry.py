from rate_limiter import RateLimiter
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from errors import StructuredOutputError, UpstreamResponseError, normalize_error
from main import app, chat, ChatInput
from metrics import MetricsStore
from retry import stream_with_retry, wait_before_retry, with_retry
from schemas import LLMEvent, LLMResult


def unavailable():
    return UpstreamResponseError('private', 'server_error')


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))
        self.wait = self.enterContext(patch('retry.wait_before_retry', new_callable=AsyncMock))

    async def test_t1_t2_t3_attempt_limits(self):
        for failures in range(5):
            with self.subTest(failures=failures):
                operation = AsyncMock(side_effect=[unavailable() for _ in range(failures)] + ['ok'])
                if failures < 4:
                    self.assertEqual(await with_retry(operation), 'ok')
                else:
                    with self.assertRaises(UpstreamResponseError):
                        await with_retry(operation)
                self.assertEqual(operation.await_count, min(failures + 1, 4))

    async def test_t4_t5_nonretryable(self):
        for error in (UpstreamResponseError('private', 'authentication_error'),
                      StructuredOutputError('bad json'), RuntimeError('bug'),
                      UpstreamResponseError('bad response')):
            operation = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await with_retry(operation)
            self.assertEqual(operation.await_count, 1)
        self.wait.assert_not_awaited()

    async def test_t6_t7_rate_limit_and_timeout(self):
        for error in (UpstreamResponseError('private', 'rate_limit_error'),
                      httpx.ReadTimeout('timeout'), UpstreamResponseError('private', 'timeout_error')):
            operation = AsyncMock(side_effect=[error, 'ok'])
            self.assertEqual(await with_retry(operation), 'ok')
            self.assertEqual(operation.await_count, 2)
        self.assertEqual(normalize_error(UpstreamResponseError('', 'timeout_error')).code, 'UPSTREAM_TIMEOUT')

    async def test_backoff_and_jitter(self):
        with patch('retry.asyncio.sleep', new_callable=AsyncMock) as sleep, patch(
            'retry.random.uniform', return_value=0.1
        ) as jitter:
            for n in (1, 2, 3, 8):
                await wait_before_retry(n)
            self.assertEqual([c.args[0] for c in sleep.await_args_list], [0.6, 1.1, 2.1, 5.1])
            jitter.assert_called_with(0, 0.2)

    async def test_t8_t11_stream_recreates_and_closes_before_wait(self):
        calls, closed = [], []

        async def source():
            number = len(calls)
            calls.append(number)
            try:
                if number < 2:
                    raise unavailable()
                yield 'ok'
            finally:
                closed.append(number)

        async def wait(n):
            self.assertEqual(len(closed), n)

        self.wait.side_effect = wait
        self.assertEqual([x async for x in stream_with_retry(source)], ['ok'])
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(closed, calls)

    async def test_stream_exhaustion(self):
        closed = []

        async def source():
            try:
                raise unavailable()
                yield
            finally:
                closed.append(True)

        with self.assertRaises(UpstreamResponseError):
            _ = [x async for x in stream_with_retry(source)]
        self.assertEqual(len(closed), 4)
        self.assertEqual(self.wait.await_count, 3)

    async def test_t9_any_forwarded_event_prevents_retry(self):
        for event in (LLMEvent(type='delta', text='hello'), LLMEvent(type='delta', text=''),
                      LLMEvent(type='usage', usage={'total_tokens': 3}),
                      LLMEvent(type='done', finish_reason='stop')):
            async def source():
                yield event
                raise unavailable()
            stream = stream_with_retry(source)
            self.assertEqual(await anext(stream), event)
            with self.assertRaises(UpstreamResponseError):
                await anext(stream)
        self.wait.assert_not_awaited()

    async def test_t10_cancel_during_wait(self):
        for streaming in (False, True):
            started = asyncio.Event()
            operation = AsyncMock(side_effect=unavailable())

            async def wait(n):
                started.set()
                await asyncio.Future()

            async def source():
                await operation()
                yield

            async def consume():
                return [x async for x in stream_with_retry(source)]

            self.wait.side_effect = wait
            task = asyncio.create_task(consume() if streaming else with_retry(operation))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(operation.await_count, 1)

    async def test_consumer_error_and_cleanup_error_do_not_retry(self):
        async def source():
            yield 'hello'
        stream = stream_with_retry(source)
        await anext(stream)
        with self.assertRaises(httpx.ReadTimeout):
            await stream.athrow(httpx.ReadTimeout('send failed'))
        failed = Mock(__anext__=AsyncMock(side_effect=unavailable()),
                      aclose=AsyncMock(side_effect=httpx.ReadTimeout('close failed')))
        with self.assertRaises(httpx.ReadTimeout):
            _ = [x async for x in stream_with_retry(lambda: failed)]
        self.wait.assert_not_awaited()


class RetryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))
        directory = self.enterContext(TemporaryDirectory())
        self.store = MetricsStore(Path(directory) / 'metrics.db')
        self.enterContext(patch('main.metrics_store', self.store))
        self.wait = self.enterContext(patch('retry.wait_before_retry', new_callable=AsyncMock))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test'))

    async def test_t12_metrics_include_wait_once_for_both_modes_and_models(self):
        for model in ('deepseek-flash', 'deepseek-v4-pro'):
            for streaming in (False, True):
                now = [0.0]
                calls = []
                async def wait(n):
                    now[0] += 0.5 * 2 ** (n - 1)
                self.wait.side_effect = wait
                async def complete(*args, **kwargs):
                    calls.append(True)
                    if len(calls) < 3:
                        raise unavailable()
                    now[0] += 0.1
                    return LLMResult(model=model, content='hi', finish_reason='stop',
                                     usage={'total_tokens': 7})
                async def source(*args, **kwargs):
                    result = await complete()
                    yield LLMEvent(type='delta', text=result.content)
                    yield LLMEvent(type='done', finish_reason='stop')
                    yield LLMEvent(type='usage', usage=result.usage)
                    now[0] += 0.2
                with patch('main.complete_reply', complete), patch('main.stream_reply', source), patch(
                    'main.perf_counter', side_effect=lambda: now[0]
                ):
                    response = await self.client.post('/chat', json={
                        'prompt': 'hi', 'model': model, 'stream': streaming})
                self.assertEqual(len(calls), 3)
                row, = self.store.recent(10, None, request_id=response.headers['X-Request-ID'])
                self.assertEqual(row['status'], 'success')
                self.assertEqual(row['total_tokens'], 7)
                self.assertAlmostEqual(row['latency_ms'], 1800 if streaming else 1600)
                if streaming:
                    self.assertAlmostEqual(row['ttft_ms'], 1600)
                    self.assertEqual(response.text.count('event: start'), 1)
                    self.assertEqual(response.text.count('event: delta'), 1)
                    self.assertTrue(response.text.rstrip().endswith('{"reason": "stop"}'))
                    self.assertNotIn('event: error', response.text)

    async def test_done_then_failure_never_sends_done(self):
        async def source(*args, **kwargs):
            yield LLMEvent(type='done', finish_reason='stop')
            raise unavailable()
        with patch('main.stream_reply', source):
            response = await self.client.post('/chat', json={'prompt': 'hi'})
        self.assertNotIn('event: done', response.text)
        self.assertEqual(response.text.count('event: error'), 1)
        self.wait.assert_not_awaited()
        row, = self.store.recent(10, None)
        self.assertEqual(row['status'], 'error')

    async def test_cancel_wait_records_once_and_closes(self):
        started, closed = asyncio.Event(), asyncio.Event()
        async def source(*args, **kwargs):
            try:
                raise unavailable()
                yield
            finally:
                closed.set()
        async def wait(n):
            started.set()
            await asyncio.Future()
        self.wait.side_effect = wait
        request = Mock(is_disconnected=AsyncMock(return_value=False))
        request.state.request_id = str(uuid4())
        with patch('main.stream_reply', source):
            response = await chat(ChatInput(prompt='hi'), request)
            await anext(response.body_iterator)
            task = asyncio.create_task(anext(response.body_iterator))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(closed.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row, = self.store.recent(10, None)
        self.assertEqual(row['status'], 'cancelled')
