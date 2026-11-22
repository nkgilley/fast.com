"""Public API and Home Assistant drop-in facade for fastdotcom."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from .client import FastDotComClient, decode_json_bytes, to_range_url
from .const import (
    DEFAULT_CONNECTIONS_MAX,
    DEFAULT_CONNECTIONS_MIN,
    DEFAULT_MAX_TIME,
    DEFAULT_MIN_TIME,
    DEFAULT_PING_COUNT,
    DEFAULT_PING_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_UPLOAD_PAYLOAD_SIZE,
    DEFAULT_URL_COUNT,
    FAST_API_V2_URL,
    FAST_COM_URL,
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
    import aiohttp

__version__ = "0.0.7"

__all__ = [
    "FastClientInfo",
    "FastDotComClient",
    "FastDotComConnectionError",
    "FastDotComError",
    "FastDotComTimeoutError",
    "FastDotComTokenError",
    "FastLocation",
    "FastSpeedtestResult",
    "FastTarget",
    "__version__",
    "async_fast_com",
    "decode_json_bytes",
    "fast_com",
    "to_range_url",
]

_LOGGER = logging.getLogger(__name__)


async def async_fast_com(
    *,
    session: aiohttp.ClientSession | None = None,
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
    force_ipv4: bool = False,
    force_ipv6: bool = False,
    request_timeout: float = DEFAULT_TIMEOUT,
    fast_com_url: str | None = None,
    api_url: str | None = None,
) -> FastSpeedtestResult:
    """Measure Internet speed asynchronously using Fast.com API v2."""
    async with FastDotComClient(
        session=session,
        timeout=request_timeout,
        force_ipv4=force_ipv4,
        force_ipv6=force_ipv6,
        fast_com_url=FAST_COM_URL if fast_com_url is None else fast_com_url,
        api_url=FAST_API_V2_URL if api_url is None else api_url,
    ) as client:
        return await client.run_speedtest(
            min_time=min_time,
            max_time=max_time,
            ping_count=ping_count,
            ping_interval=ping_interval,
            url_count=url_count,
            connections_min=connections_min,
            connections_max=connections_max,
            upload_payload_size=upload_payload_size,
            measure_download=measure_download,
            measure_upload=measure_upload,
            measure_latency=measure_latency,
        )


def fast_com(
    verbose: bool = False,
    max_time: float = DEFAULT_MAX_TIME,
    ping_count: int = DEFAULT_PING_COUNT,
    force_ipv4: bool = False,
    force_ipv6: bool = False,
    payload_size: int | None = None,
    max_payload_size: int | None = None,
) -> dict[str, float]:
    """Synchronous drop-in compatible speedtest entrypoint for Home Assistant."""
    if payload_size is not None and payload_size <= 0:
        raise ValueError("payload_size must be positive")
    if max_payload_size is not None and max_payload_size <= 0:
        raise ValueError("max_payload_size must be positive")

    effective_min_time = min(DEFAULT_MIN_TIME, max_time)
    effective_payload_size = DEFAULT_UPLOAD_PAYLOAD_SIZE
    if payload_size is not None and max_payload_size is not None:
        effective_payload_size = min(payload_size, max_payload_size)
    elif payload_size is not None:
        effective_payload_size = payload_size
    elif max_payload_size is not None:
        effective_payload_size = max_payload_size

    async def _run() -> FastSpeedtestResult:
        return await async_fast_com(
            min_time=effective_min_time,
            max_time=max_time,
            ping_count=ping_count,
            force_ipv4=force_ipv4,
            force_ipv6=force_ipv6,
            upload_payload_size=effective_payload_size,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result = asyncio.run(_run())
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(asyncio.run, _run()).result()

    if verbose:
        _LOGGER.info(
            "Fast.com speedtest complete: download=%.2f Mbps, upload=%.2f Mbps, "
            "unloaded_ping=%.2f ms, loaded_ping=%.2f ms",
            result.download_speed,
            result.upload_speed,
            result.unloaded_ping,
            result.loaded_ping,
        )

    return result.to_dict()
