"""Offline loopback test server and shared fixtures for fastdotcom."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web

if TYPE_CHECKING:
    from collections.abc import Iterator


class _LoopThread:
    """Dedicated background event loop thread so sync and async tests never deadlock."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def run_coro(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=10.0)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5.0)
        self.loop.close()


class MockFastServer:
    """Configurable in-process aiohttp server simulating Fast.com and Netflix OCAs."""

    def __init__(self, loop_thread: _LoopThread) -> None:
        self._loop_thread = loop_thread
        self._runner: web.AppRunner | None = None
        self.base_url = ""

        # Recorded telemetry
        self.requests: list[tuple[str, str]] = []
        self.v2_calls = 0
        self.upload_bytes_received = 0
        self.upload_headers_seen: list[dict[str, str]] = []

        # Configurable responses
        self.html_status = 200
        self.html_body: str | None = None
        self.html_delay = 0.0

        self.js_status = 200
        self.js_body: str | None = None
        self.js_delay = 0.0

        self.api_status = 200
        self.api_raw_bytes: bytes | None = None
        self.api_delay = 0.0

        self.download_status = 200
        self.download_chunk_size = 64 * 1024
        self.download_chunks = 16
        self.download_chunk_delay = 0.0

        self.range_get_status = 200
        self.range_get_delay = 0.0
        self.range_get_fail_times = 0

        self.upload_status = 200
        self.upload_delay = 0.0
        self.enforce_content_length = True

    @property
    def fast_com_url(self) -> str:
        return f"{self.base_url}/"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/netflix/speedtest/v2"

    @property
    def target_url(self) -> str:
        return f"{self.base_url}/speedtest?c=es&n=64512&v=1"

    def default_v2_bytes(self) -> bytes:
        payload = {
            "client": {
                "ip": "198.51.100.42",
                "asn": "64512",
                "isp": "Example Fiber",
                "location": {"city": "Málaga", "country": "ES"},
            },
            "targets": [
                {
                    "name": "oca-1",
                    "url": f"{self.base_url}/speedtest?c=es&n=64512&v=1",
                    "location": {"city": "Málaga", "country": "ES"},
                },
                {
                    "name": "oca-2",
                    "url": f"{self.base_url}/speedtest?c=es&n=64512&v=2",
                    "location": {"city": "Madrid", "country": "ES"},
                },
            ],
        }
        return json.dumps(payload, ensure_ascii=False).encode("latin-1")

    async def _handle_html(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path))
        if self.html_delay > 0:
            await asyncio.sleep(self.html_delay)
        if self.html_status != 200:
            return web.Response(status=self.html_status, text="Error")
        body = (
            self.html_body
            if self.html_body is not None
            else '<html><head><script src="/app-test.js"></script></head></html>'
        )
        return web.Response(status=200, text=body, content_type="text/html")

    async def _handle_js(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path))
        if self.js_delay > 0:
            await asyncio.sleep(self.js_delay)
        if self.js_status != 200:
            return web.Response(status=self.js_status, text="Error")
        body = (
            self.js_body
            if self.js_body is not None
            else 'var cfg={api:"https://api.fast.com",token:"test-token-1234"};'
        )
        return web.Response(
            status=200, text=body, content_type="application/javascript"
        )

    async def _handle_api_v2(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path))
        self.v2_calls += 1
        if self.api_delay > 0:
            await asyncio.sleep(self.api_delay)
        if self.api_status != 200:
            return web.Response(status=self.api_status, text="API error")
        raw = (
            self.api_raw_bytes
            if self.api_raw_bytes is not None
            else self.default_v2_bytes()
        )
        return web.Response(
            status=200,
            body=raw,
            headers={"Content-Type": "application/json"},
        )

    async def _handle_download(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path))
        if self.download_status >= 400:
            return web.Response(status=self.download_status, text="Download error")
        resp = web.StreamResponse(status=self.download_status)
        await resp.prepare(request)
        chunk = b"x" * self.download_chunk_size
        try:
            for _ in range(self.download_chunks):
                if self.download_chunk_delay > 0:
                    await asyncio.sleep(self.download_chunk_delay)
                await resp.write(chunk)
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _handle_range_get(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path))
        if self.range_get_delay > 0:
            await asyncio.sleep(self.range_get_delay)
        if self.range_get_fail_times > 0:
            self.range_get_fail_times -= 1
            return web.Response(status=503, text="Transient probe error")
        if self.range_get_status >= 400:
            return web.Response(status=self.range_get_status, text="Probe error")
        return web.Response(status=self.range_get_status, body=b"0")

    async def _handle_range_post(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path))
        self.upload_headers_seen.append(dict(request.headers))
        transfer_encoding = request.headers.get("Transfer-Encoding", "")
        content_length = request.headers.get("Content-Length")
        if self.enforce_content_length and (
            "chunked" in transfer_encoding.lower() or content_length is None
        ):
            return web.Response(status=406, text="Not Acceptable")
        try:
            body = await request.read()
        except (ConnectionResetError, asyncio.CancelledError):
            return web.Response(status=499)
        self.upload_bytes_received += len(body)
        if self.upload_delay > 0:
            await asyncio.sleep(self.upload_delay)
        return web.Response(status=self.upload_status)

    async def _start_async(self) -> None:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get("/", self._handle_html)
        app.router.add_get("/app-test.js", self._handle_js)
        app.router.add_get("/netflix/speedtest/v2", self._handle_api_v2)
        app.router.add_get("/speedtest", self._handle_download)
        app.router.add_get("/speedtest/range/0-0", self._handle_range_get)
        app.router.add_post("/speedtest/range/0-0", self._handle_range_post)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = int(runner.addresses[0][1])
        self.base_url = f"http://127.0.0.1:{port}"
        self._runner = runner

    async def _stop_async(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def start(self) -> None:
        self._loop_thread.run_coro(self._start_async())

    def stop(self) -> None:
        self._loop_thread.run_coro(self._stop_async())


@pytest.fixture
def mock_fast_server() -> Iterator[MockFastServer]:
    """Start an isolated loopback Fast.com mock server on a background thread."""
    loop_thread = _LoopThread()
    server = MockFastServer(loop_thread)
    server.start()
    try:
        yield server
    finally:
        server.stop()
        loop_thread.stop()
