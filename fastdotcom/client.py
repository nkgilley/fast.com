"""Asynchronous Fast.com API v2 speedtest client."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import urllib.parse
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import (
    DEFAULT_CONNECTIONS_MAX,
    DEFAULT_CONNECTIONS_MIN,
    DEFAULT_DOWNLOAD_CHUNK_SIZE,
    DEFAULT_MAX_TIME,
    DEFAULT_MIN_TIME,
    DEFAULT_PING_COUNT,
    DEFAULT_PING_INTERVAL,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_SAMPLE_INTERVAL,
    DEFAULT_STABILITY_SAMPLES,
    DEFAULT_STABILITY_THRESHOLD,
    DEFAULT_TIMEOUT,
    DEFAULT_UPLOAD_CHUNK_SIZE,
    DEFAULT_UPLOAD_PAYLOAD_SIZE,
    DEFAULT_URL_COUNT,
    DEFAULT_WINDOW_SIZE,
    FAST_API_V2_URL,
    FAST_COM_URL,
    SCRIPT_SRC_RE,
    SPEED_THRESHOLD_3_CONN_MBPS,
    SPEED_THRESHOLD_5_CONN_MBPS,
    SPEED_THRESHOLD_MAX_CONN_MBPS,
    SPEEDTEST_PATH_RE,
    TOKEN_RE,
)
from .exceptions import (
    FastDotComConnectionError,
    FastDotComError,
    FastDotComTimeoutError,
    FastDotComTokenError,
)
from .models import (
    FastClientInfo,
    FastLocation,
    FastSpeedtestResult,
    FastTarget,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
    from types import TracebackType

    from aiohttp.client_proto import ResponseHandler
    from aiohttp.client_reqrep import ConnectionKey

_LOGGER = logging.getLogger(__name__)


def decode_json_bytes(raw: bytes) -> Any:
    """Decode JSON bytes using UTF-8 with latin-1 fallback."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FastDotComConnectionError(
            f"Invalid JSON payload from Fast.com: {exc}"
        ) from exc


def to_range_url(url: str, start: int = 0, end: int = 0) -> str:
    """Rewrite a Fast.com OCA target URL to a byte-range endpoint."""
    if start < 0 or end < start:
        raise ValueError(f"Invalid byte range {start}-{end}")
    parts = urllib.parse.urlsplit(url)
    suffix = f"range/{start}-{end}"
    if SPEEDTEST_PATH_RE.search(parts.path):
        new_path = SPEEDTEST_PATH_RE.sub(f"/speedtest/{suffix}", parts.path)
    elif parts.path.endswith("/"):
        new_path = f"{parts.path}{suffix}"
    else:
        new_path = f"{parts.path}/{suffix}"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
    )


def _desired_connections(
    speed_mbps: float,
    connections_min: int,
    connections_max: int,
) -> int:
    """Compute target parallel worker count from observed Mbps."""
    if speed_mbps > SPEED_THRESHOLD_MAX_CONN_MBPS:
        target = connections_max
    elif speed_mbps > SPEED_THRESHOLD_5_CONN_MBPS:
        target = min(5, connections_max)
    elif speed_mbps > SPEED_THRESHOLD_3_CONN_MBPS:
        target = min(3, connections_max)
    else:
        target = connections_min
    return max(connections_min, min(target, connections_max))


def _parse_location(raw: Any) -> FastLocation | None:
    """Parse optional location object into FastLocation."""
    if not isinstance(raw, dict):
        return None
    city = raw.get("city")
    country = raw.get("country")
    return FastLocation(
        city=str(city) if city is not None else None,
        country=str(country) if country is not None else None,
    )


def _parse_client_info(raw: Any) -> FastClientInfo | None:
    """Parse optional client metadata object into FastClientInfo."""
    if not isinstance(raw, dict):
        return None
    ip = raw.get("ip")
    asn = raw.get("asn")
    isp = raw.get("isp")
    return FastClientInfo(
        ip=str(ip) if ip is not None else None,
        asn=str(asn) if asn is not None else None,
        isp=str(isp) if isp is not None else None,
        location=_parse_location(raw.get("location")),
    )


def _parse_targets_list(raw_targets: list[Any]) -> list[FastTarget]:
    """Parse target entries into a validated list of FastTarget objects."""
    targets: list[FastTarget] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        name = item.get("name")
        targets.append(
            FastTarget(
                url=url,
                name=str(name) if name is not None else None,
                location=_parse_location(item.get("location")),
            )
        )
    if not targets:
        raise FastDotComConnectionError("Fast.com returned no valid speedtest targets")
    return targets


def _extract_target_urls(targets: Sequence[FastTarget | str]) -> list[str]:
    """Normalize FastTarget or URL strings into a non-empty list of URLs."""
    urls = [t.url if isinstance(t, FastTarget) else t for t in targets]
    if not urls or any(not u for u in urls):
        raise ValueError("targets must be a non-empty sequence of valid URLs")
    return urls


def _validate_transfer_bounds(
    min_time: float,
    max_time: float,
    connections_min: int,
    connections_max: int,
    chunk_size: int,
    window_size: int,
    sample_interval: float,
) -> None:
    """Validate common transfer timing, connection, and window parameters."""
    if min_time <= 0 or max_time < min_time:
        raise ValueError("Require 0 < min_time <= max_time")
    if connections_min < 1 or connections_max < connections_min:
        raise ValueError("Require 1 <= connections_min <= connections_max")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if window_size < 1:
        raise ValueError("window_size must be at least 1")
    if sample_interval <= 0:
        raise ValueError("sample_interval must be positive")


def _wrap_worker_error(exc: BaseException) -> FastDotComError:
    """Normalize an exception captured from a transfer worker."""
    if isinstance(exc, FastDotComError):
        return exc
    if isinstance(exc, TimeoutError):
        return FastDotComTimeoutError(str(exc) or "Fast.com transfer timed out")
    return FastDotComConnectionError(str(exc) or "Fast.com transfer failed")


class _FastTCPConnector(aiohttp.TCPConnector):
    """TCPConnector that aborts SSL transports on forced release to avoid leaks."""

    def _release(
        self,
        key: ConnectionKey,
        protocol: ResponseHandler,
        *,
        should_close: bool = False,
    ) -> None:
        """Cancel orphaned drain waiter and abort SSL transport on forced release."""
        if should_close:
            waiter = getattr(protocol, "_drain_waiter", None)
            if isinstance(waiter, asyncio.Future) and not waiter.done():
                waiter.cancel()
            if key.is_ssl and protocol.transport is not None:
                protocol.transport.abort()
        super()._release(key, protocol, should_close=should_close)


class FastDotComClient:
    """Async Fast.com API v2 speedtest client with session ownership safety."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        force_ipv4: bool = False,
        force_ipv6: bool = False,
        fast_com_url: str = FAST_COM_URL,
        api_url: str = FAST_API_V2_URL,
    ) -> None:
        """Initialize the Fast.com client."""
        if force_ipv4 and force_ipv6:
            raise ValueError("Cannot force both IPv4 and IPv6 simultaneously")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout
        self._force_ipv4 = force_ipv4
        self._force_ipv6 = force_ipv6
        self._fast_com_url = fast_com_url
        self._api_url = api_url

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the injected session or lazily create an owned ClientSession."""
        if self._session is not None and (
            not self._owns_session or not self._session.closed
        ):
            return self._session
        family = socket.AF_UNSPEC
        if self._force_ipv4:
            family = socket.AF_INET
        elif self._force_ipv6:
            family = socket.AF_INET6
        connector = _FastTCPConnector(family=family)
        self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session only if created internally."""
        if (
            self._owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()

    async def __aenter__(self) -> FastDotComClient:
        """Enter async context manager."""
        self._ensure_session()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context manager cleanly."""
        await self.close()

    async def get_token(self) -> str:
        """Fetch fast.com HTML, locate its JS bundle, and extract the API token."""
        session = self._ensure_session()
        try:
            async with asyncio.timeout(self._timeout):
                async with session.get(self._fast_com_url) as resp:
                    if resp.status != 200:
                        raise FastDotComTokenError(
                            f"Failed to fetch {self._fast_com_url}: HTTP {resp.status}"
                        )
                    html = await resp.text()
                script_match = SCRIPT_SRC_RE.search(html)
                if not script_match:
                    raise FastDotComTokenError(
                        "Could not locate Fast.com JavaScript bundle in HTML"
                    )
                js_url = urllib.parse.urljoin(self._fast_com_url, script_match.group(1))
                async with session.get(js_url) as js_resp:
                    if js_resp.status != 200:
                        raise FastDotComTokenError(
                            f"Failed to fetch JS bundle {js_url}: HTTP {js_resp.status}"
                        )
                    js_body = await js_resp.text()
        except TimeoutError as exc:
            raise FastDotComTimeoutError(
                "Timed out fetching Fast.com public token"
            ) from exc
        except aiohttp.ClientError as exc:
            raise FastDotComConnectionError(
                f"Network error fetching Fast.com token: {exc}"
            ) from exc

        token_match = TOKEN_RE.search(js_body)
        if not token_match:
            raise FastDotComTokenError("Token not found in Fast.com JavaScript bundle")
        token = token_match.group(1)
        _LOGGER.debug("Extracted Fast.com token (%d chars)", len(token))
        return token

    async def get_targets(
        self,
        token: str | None = None,
        *,
        url_count: int = DEFAULT_URL_COUNT,
    ) -> tuple[FastClientInfo | None, list[FastTarget]]:
        """Discover speedtest targets (and client metadata) via a single API v2 call."""
        if url_count < 1:
            raise ValueError("url_count must be at least 1")
        resolved_token = token if token is not None else await self.get_token()
        session = self._ensure_session()
        params = {
            "https": "true",
            "token": resolved_token,
            "urlCount": str(url_count),
        }
        try:
            async with asyncio.timeout(self._timeout):
                async with session.get(self._api_url, params=params) as resp:
                    if resp.status != 200:
                        raise FastDotComConnectionError(
                            f"Fast.com API returned HTTP {resp.status}"
                        )
                    raw = await resp.read()
        except TimeoutError as exc:
            raise FastDotComTimeoutError(
                "Timed out fetching Fast.com speedtest targets"
            ) from exc
        except aiohttp.ClientError as exc:
            raise FastDotComConnectionError(
                f"Network error fetching Fast.com targets: {exc}"
            ) from exc

        payload = decode_json_bytes(raw)
        if isinstance(payload, dict):
            raw_targets = payload.get("targets")
            if not isinstance(raw_targets, list):
                raise FastDotComConnectionError(
                    "Fast.com API v2 response missing 'targets' list"
                )
            return _parse_client_info(payload.get("client")), _parse_targets_list(
                raw_targets
            )
        if isinstance(payload, list):
            return None, _parse_targets_list(payload)
        raise FastDotComConnectionError(
            "Unexpected JSON structure from Fast.com API endpoint"
        )

    async def measure_latency(
        self,
        url: str,
        *,
        count: int = DEFAULT_PING_COUNT,
        warmup: bool = True,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> tuple[float, list[float]]:
        """Measure HTTP RTT (ms) via keep-alive GET /speedtest/range/0-0 probes."""
        if count < 1:
            raise ValueError("count must be at least 1")
        if probe_timeout <= 0:
            raise ValueError("probe_timeout must be positive")
        session = self._ensure_session()
        range_url = to_range_url(url, 0, 0)
        samples: list[float] = []

        async def _single_probe() -> float:
            try:
                async with asyncio.timeout(probe_timeout):
                    start = time.perf_counter()
                    async with session.get(range_url) as resp:
                        if resp.status >= 400:
                            raise FastDotComConnectionError(
                                f"Latency probe failed with HTTP {resp.status}"
                            )
                        await resp.read()
                    return (time.perf_counter() - start) * 1000.0
            except TimeoutError as exc:
                raise FastDotComTimeoutError("HTTP latency probe timed out") from exc
            except aiohttp.ClientError as exc:
                raise FastDotComConnectionError(
                    f"HTTP latency probe failed: {exc}"
                ) from exc

        if warmup:
            await _single_probe()
        for _ in range(count):
            samples.append(await _single_probe())

        avg_ms = sum(samples) / len(samples)
        return avg_ms, samples

    async def _orchestrate_transfer(
        self,
        target_urls: list[str],
        *,
        make_worker: Callable[[str], Coroutine[Any, Any, None]],
        get_bytes: Callable[[], int],
        get_error: Callable[[], BaseException | None],
        stop_event: asyncio.Event,
        min_time: float,
        max_time: float,
        connections_min: int,
        connections_max: int,
        window_size: int,
        sample_interval: float,
        extra_tasks: Sequence[asyncio.Task[Any]] = (),
    ) -> tuple[float, int]:
        """Run dynamic-scaling transfer workers and monitor sliding-window Mbps."""
        start_time = time.perf_counter()
        workers: list[asyncio.Task[None]] = []
        spawn_index = 0

        def _spawn_worker() -> None:
            nonlocal spawn_index
            url = target_urls[spawn_index % len(target_urls)]
            spawn_index += 1
            workers.append(asyncio.create_task(make_worker(url)))

        for _ in range(connections_min):
            _spawn_worker()

        samples: deque[tuple[float, int]] = deque(maxlen=window_size + 1)
        samples.append((start_time, 0))
        recent_speeds: deque[float] = deque(maxlen=DEFAULT_STABILITY_SAMPLES)
        peak_mbps = 0.0

        try:
            while not stop_event.is_set():
                elapsed = time.perf_counter() - start_time
                remaining = max_time - elapsed
                if remaining <= 0:
                    break
                await asyncio.sleep(min(sample_interval, remaining))
                now = time.perf_counter()
                elapsed = now - start_time
                current_bytes = get_bytes()
                samples.append((now, current_bytes))

                base_time, base_bytes = samples[0]
                dt = now - base_time
                db = current_bytes - base_bytes
                speed_mbps = (db * 8.0) / (dt * 1_000_000.0) if dt > 0 else 0.0
                if speed_mbps > peak_mbps:
                    peak_mbps = speed_mbps

                desired = _desired_connections(
                    speed_mbps, connections_min, connections_max
                )
                while len(workers) < desired:
                    _spawn_worker()

                if all(worker.done() for worker in workers):
                    break

                if elapsed >= min_time:
                    recent_speeds.append(speed_mbps)
                    if (
                        len(recent_speeds) == DEFAULT_STABILITY_SAMPLES
                        and max(recent_speeds) > 0
                    ):
                        spread = (max(recent_speeds) - min(recent_speeds)) / max(
                            recent_speeds
                        )
                        if spread <= DEFAULT_STABILITY_THRESHOLD:
                            break
        finally:
            stop_event.set()
            all_tasks: list[asyncio.Task[Any]] = [*workers, *extra_tasks]
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

        final_bytes = get_bytes()
        if final_bytes == 0:
            err = get_error()
            if err is not None:
                raise _wrap_worker_error(err)
            raise FastDotComConnectionError("Transfer completed with 0 bytes")

        if peak_mbps == 0.0:
            total_elapsed = max(time.perf_counter() - start_time, 1e-9)
            peak_mbps = (final_bytes * 8.0) / (total_elapsed * 1_000_000.0)

        return peak_mbps, final_bytes

    async def measure_download(
        self,
        targets: Sequence[FastTarget | str],
        *,
        min_time: float = DEFAULT_MIN_TIME,
        max_time: float = DEFAULT_MAX_TIME,
        connections_min: int = DEFAULT_CONNECTIONS_MIN,
        connections_max: int = DEFAULT_CONNECTIONS_MAX,
        chunk_size: int = DEFAULT_DOWNLOAD_CHUNK_SIZE,
        window_size: int = DEFAULT_WINDOW_SIZE,
        sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
        measure_loaded_latency: bool = True,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> tuple[float, int, float, list[float]]:
        """Measure download speed (Mbps) and optional concurrent loaded latency (ms)."""
        target_urls = _extract_target_urls(targets)
        _validate_transfer_bounds(
            min_time,
            max_time,
            connections_min,
            connections_max,
            chunk_size,
            window_size,
            sample_interval,
        )
        if ping_interval <= 0 or probe_timeout <= 0:
            raise ValueError("ping_interval and probe_timeout must be positive")

        session = self._ensure_session()
        stop_event = asyncio.Event()
        download_started = asyncio.Event()
        total_bytes = 0
        first_error: BaseException | None = None
        loaded_pings: list[float] = []
        req_timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self._timeout,
            sock_read=self._timeout,
        )

        def _record_error(exc: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = exc

        async def _download_worker(url: str) -> None:
            nonlocal total_bytes
            try:
                while not stop_event.is_set():
                    async with session.get(url, timeout=req_timeout) as resp:
                        if resp.status >= 400:
                            _record_error(
                                FastDotComConnectionError(
                                    f"Download failed with HTTP {resp.status}"
                                )
                            )
                            return
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            total_bytes += len(chunk)
                            download_started.set()
                            if stop_event.is_set():
                                return
            except (FastDotComError, aiohttp.ClientError, TimeoutError) as exc:
                if not stop_event.is_set():
                    _record_error(exc)

        async def _loaded_latency_probe(probe_url: str) -> None:
            await download_started.wait()
            while not stop_event.is_set():
                try:
                    _, samples = await self.measure_latency(
                        probe_url,
                        count=1,
                        warmup=False,
                        probe_timeout=probe_timeout,
                    )
                    loaded_pings.extend(samples)
                except FastDotComError as exc:
                    _LOGGER.debug("Transient loaded latency probe error: %s", exc)
                if stop_event.is_set():
                    break
                await asyncio.sleep(ping_interval)

        extra_tasks: list[asyncio.Task[Any]] = []
        if measure_loaded_latency:
            extra_tasks.append(
                asyncio.create_task(_loaded_latency_probe(target_urls[0]))
            )

        peak_mbps, final_bytes = await self._orchestrate_transfer(
            target_urls,
            make_worker=_download_worker,
            get_bytes=lambda: total_bytes,
            get_error=lambda: first_error,
            stop_event=stop_event,
            min_time=min_time,
            max_time=max_time,
            connections_min=connections_min,
            connections_max=connections_max,
            window_size=window_size,
            sample_interval=sample_interval,
            extra_tasks=extra_tasks,
        )

        avg_loaded_ping = sum(loaded_pings) / len(loaded_pings) if loaded_pings else 0.0
        return peak_mbps, final_bytes, avg_loaded_ping, loaded_pings

    async def measure_upload(
        self,
        targets: Sequence[FastTarget | str],
        *,
        min_time: float = DEFAULT_MIN_TIME,
        max_time: float = DEFAULT_MAX_TIME,
        connections_min: int = DEFAULT_CONNECTIONS_MIN,
        connections_max: int = DEFAULT_CONNECTIONS_MAX,
        chunk_size: int = DEFAULT_UPLOAD_CHUNK_SIZE,
        payload_size: int = DEFAULT_UPLOAD_PAYLOAD_SIZE,
        window_size: int = DEFAULT_WINDOW_SIZE,
        sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
    ) -> tuple[float, int]:
        """Measure upload speed (Mbps) streaming a pre-allocated reusable buffer."""
        target_urls = _extract_target_urls(targets)
        _validate_transfer_bounds(
            min_time,
            max_time,
            connections_min,
            connections_max,
            chunk_size,
            window_size,
            sample_interval,
        )
        if payload_size <= 0:
            raise ValueError("payload_size must be positive")

        session = self._ensure_session()
        stop_event = asyncio.Event()
        total_bytes = 0
        first_error: BaseException | None = None
        reusable_chunk = bytes(min(chunk_size, payload_size))
        req_timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self._timeout,
            sock_read=self._timeout,
        )
        upload_headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(payload_size),
        }

        def _record_error(exc: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = exc

        async def _upload_worker(url: str) -> None:
            nonlocal total_bytes
            upload_url = to_range_url(url, 0, 0)
            try:
                while not stop_event.is_set():
                    sent_in_req = 0

                    async def _payload_stream() -> AsyncIterator[bytes]:
                        nonlocal sent_in_req, total_bytes
                        remaining = payload_size
                        chunk_len = len(reusable_chunk)
                        while remaining > 0:
                            if stop_event.is_set():
                                raise asyncio.CancelledError
                            piece = (
                                reusable_chunk
                                if remaining >= chunk_len
                                else reusable_chunk[:remaining]
                            )
                            yield piece
                            piece_len = len(piece)
                            sent_in_req += piece_len
                            total_bytes += piece_len
                            remaining -= piece_len

                    async with session.post(
                        upload_url,
                        data=_payload_stream(),
                        headers=upload_headers,
                        timeout=req_timeout,
                    ) as resp:
                        if resp.status >= 400:
                            total_bytes = max(0, total_bytes - sent_in_req)
                            _record_error(
                                FastDotComConnectionError(
                                    f"Upload failed with HTTP {resp.status}"
                                )
                            )
                            return
                        await resp.read()
            except (FastDotComError, aiohttp.ClientError, TimeoutError) as exc:
                if not stop_event.is_set():
                    _record_error(exc)

        return await self._orchestrate_transfer(
            target_urls,
            make_worker=_upload_worker,
            get_bytes=lambda: total_bytes,
            get_error=lambda: first_error,
            stop_event=stop_event,
            min_time=min_time,
            max_time=max_time,
            connections_min=connections_min,
            connections_max=connections_max,
            window_size=window_size,
            sample_interval=sample_interval,
        )

    async def run_speedtest(
        self,
        *,
        min_time: float = DEFAULT_MIN_TIME,
        max_time: float = DEFAULT_MAX_TIME,
        ping_count: int = DEFAULT_PING_COUNT,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        url_count: int = DEFAULT_URL_COUNT,
        connections_min: int = DEFAULT_CONNECTIONS_MIN,
        connections_max: int = DEFAULT_CONNECTIONS_MAX,
        upload_payload_size: int = DEFAULT_UPLOAD_PAYLOAD_SIZE,
        measure_download: bool = True,
        measure_upload: bool = True,
        measure_latency: bool = True,
    ) -> FastSpeedtestResult:
        """Run a full Fast.com speedtest with a single v2 target discovery call."""
        started = time.perf_counter()
        token = await self.get_token()
        client_info, targets = await self.get_targets(token, url_count=url_count)

        unloaded_ping = 0.0
        unloaded_pings: list[float] = []
        if measure_latency:
            unloaded_ping, unloaded_pings = await self.measure_latency(
                targets[0].url,
                count=ping_count,
                warmup=True,
            )

        download_speed = 0.0
        bytes_downloaded = 0
        loaded_ping = 0.0
        loaded_pings: list[float] = []
        if measure_download:
            (
                download_speed,
                bytes_downloaded,
                loaded_ping,
                loaded_pings,
            ) = await self.measure_download(
                targets,
                min_time=min_time,
                max_time=max_time,
                connections_min=connections_min,
                connections_max=connections_max,
                measure_loaded_latency=measure_latency,
                ping_interval=ping_interval,
            )

        upload_speed = 0.0
        bytes_uploaded = 0
        if measure_upload:
            upload_speed, bytes_uploaded = await self.measure_upload(
                targets,
                min_time=min_time,
                max_time=max_time,
                connections_min=connections_min,
                connections_max=connections_max,
                payload_size=upload_payload_size,
            )

        elapsed_seconds = time.perf_counter() - started
        return FastSpeedtestResult(
            download_speed=round(download_speed, 2),
            upload_speed=round(upload_speed, 2),
            unloaded_ping=round(unloaded_ping, 2),
            loaded_ping=round(loaded_ping, 2),
            unloaded_pings=tuple(round(v, 2) for v in unloaded_pings),
            loaded_pings=tuple(round(v, 2) for v in loaded_pings),
            bytes_downloaded=bytes_downloaded,
            bytes_uploaded=bytes_uploaded,
            client=client_info,
            targets=tuple(targets),
            elapsed_seconds=round(elapsed_seconds, 2),
        )
