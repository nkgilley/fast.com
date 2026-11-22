"""Unit tests for FastDotComClient, helpers, async_fast_com, and fast_com."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import aiohttp
import pytest

import fastdotcom
from fastdotcom import (
    FastDotComClient,
    FastDotComConnectionError,
    FastDotComTimeoutError,
    FastDotComTokenError,
    FastTarget,
    async_fast_com,
    decode_json_bytes,
    fast_com,
    to_range_url,
)
from fastdotcom.client import _desired_connections, _FastTCPConnector

if TYPE_CHECKING:
    from .conftest import MockFastServer


def test_decode_json_bytes_utf8_and_latin1_and_invalid() -> None:
    """decode_json_bytes decodes UTF-8, falls back to latin-1, and wraps errors."""
    utf8_bytes = '{"city": "São Paulo"}'.encode()
    assert decode_json_bytes(utf8_bytes) == {"city": "São Paulo"}

    latin1_bytes = b'{"city": "M\xe1laga"}'
    assert decode_json_bytes(latin1_bytes) == {"city": "Málaga"}

    with pytest.raises(FastDotComConnectionError, match="Invalid JSON payload"):
        decode_json_bytes(b"{not-valid-json")


def test_to_range_url_variants_and_validation() -> None:
    """to_range_url rewrites speedtest/range paths and rejects invalid bounds."""
    assert (
        to_range_url("https://oca.example.net/speedtest?c=es&n=123", 0, 0)
        == "https://oca.example.net/speedtest/range/0-0?c=es&n=123"
    )
    assert (
        to_range_url("https://oca.example.net/speedtest/range/0-100?tok=abc", 10, 20)
        == "https://oca.example.net/speedtest/range/10-20?tok=abc"
    )
    assert (
        to_range_url("https://oca.example.net/", 0, 0)
        == "https://oca.example.net/range/0-0"
    )
    assert (
        to_range_url("https://oca.example.net/custom#frag", 1, 5)
        == "https://oca.example.net/custom/range/1-5#frag"
    )

    with pytest.raises(ValueError, match="Invalid byte range"):
        to_range_url("https://oca.example.net/speedtest", -1, 0)
    with pytest.raises(ValueError, match="Invalid byte range"):
        to_range_url("https://oca.example.net/speedtest", 10, 5)


def test_desired_connections_scaling_bands() -> None:
    """_desired_connections scales smoothly and respects min/max bounds."""
    assert _desired_connections(0.5, 1, 8) == 1
    assert _desired_connections(5.0, 1, 8) == 3
    assert _desired_connections(25.0, 1, 8) == 5
    assert _desired_connections(80.0, 1, 8) == 8
    assert _desired_connections(80.0, 2, 4) == 4
    assert _desired_connections(0.2, 4, 8) == 4


def test_client_init_validation() -> None:
    """FastDotComClient rejects contradictory IP flags and non-positive timeouts."""
    with pytest.raises(ValueError, match="both IPv4 and IPv6"):
        FastDotComClient(force_ipv4=True, force_ipv6=True)

    with pytest.raises(ValueError, match="timeout must be positive"):
        FastDotComClient(timeout=0)


async def test_session_ownership_and_ip_family(
    mock_fast_server: MockFastServer,
) -> None:
    """Injected sessions stay open; owned sessions close and respect IP families."""
    injected = aiohttp.ClientSession()
    try:
        async with FastDotComClient(
            session=injected,
            fast_com_url=mock_fast_server.fast_com_url,
            api_url=mock_fast_server.api_url,
        ) as client:
            token = await client.get_token()
            assert token == "test-token-1234"
        assert not injected.closed
    finally:
        await injected.close()

    owned_client = FastDotComClient(
        force_ipv4=True,
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    )
    async with owned_client:
        s1 = owned_client._ensure_session()
        assert isinstance(s1.connector, aiohttp.TCPConnector)
        assert s1.connector._family == socket.AF_INET
    assert s1.closed

    # Re-entering after close lazily creates a fresh owned session.
    async with owned_client:
        s2 = owned_client._ensure_session()
        assert s2 is not s1
        was_open = not s2.closed
        assert was_open
    assert s2.closed

    ipv6_client = FastDotComClient(force_ipv6=True)
    try:
        s6 = ipv6_client._ensure_session()
        assert isinstance(s6.connector, aiohttp.TCPConnector)
        assert s6.connector._family == socket.AF_INET6
    finally:
        await ipv6_client.close()


async def test_get_token_success_and_failures(mock_fast_server: MockFastServer) -> None:
    """get_token extracts tokens and raises typed errors on failures."""
    async with FastDotComClient(
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as client:
        assert await client.get_token() == "test-token-1234"

        mock_fast_server.html_status = 503
        with pytest.raises(FastDotComTokenError, match="HTTP 503"):
            await client.get_token()
        mock_fast_server.html_status = 200

        mock_fast_server.html_body = "<html><body>No scripts</body></html>"
        with pytest.raises(FastDotComTokenError, match="JavaScript bundle"):
            await client.get_token()
        mock_fast_server.html_body = None

        mock_fast_server.js_status = 404
        with pytest.raises(FastDotComTokenError, match="HTTP 404"):
            await client.get_token()
        mock_fast_server.js_status = 200

        mock_fast_server.js_body = "var x = 1;"
        with pytest.raises(FastDotComTokenError, match="Token not found"):
            await client.get_token()
        mock_fast_server.js_body = None

    async with FastDotComClient(
        timeout=0.05,
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as slow_client:
        mock_fast_server.html_delay = 0.2
        with pytest.raises(FastDotComTimeoutError, match="Timed out fetching"):
            await slow_client.get_token()
        mock_fast_server.html_delay = 0.0

    async with FastDotComClient(
        fast_com_url="http://127.0.0.1:1/",
        api_url=mock_fast_server.api_url,
    ) as bad_client:
        with pytest.raises(FastDotComConnectionError, match="Network error fetching"):
            await bad_client.get_token()


async def test_get_targets_v2_latin1_v1_and_errors(
    mock_fast_server: MockFastServer,
) -> None:
    """get_targets decodes Latin-1 v2 payloads, supports v1 list, and handles errors."""
    async with FastDotComClient(
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as client:
        with pytest.raises(ValueError, match="url_count must be at least 1"):
            await client.get_targets(url_count=0)

        client_info, targets = await client.get_targets()
        assert client_info is not None
        assert client_info.ip == "198.51.100.42"
        assert client_info.asn == "64512"
        assert client_info.isp == "Example Fiber"
        assert client_info.location is not None
        assert client_info.location.city == "Málaga"
        assert client_info.location.country == "ES"
        assert len(targets) == 2
        assert targets[0].name == "oca-1"
        assert targets[0].location is not None
        assert targets[0].location.city == "Málaga"

        # Legacy v1 list payload fallback + malformed items skipped
        v1_payload = [
            "ignored-non-dict",
            {"url": ""},
            {"url": mock_fast_server.target_url},
        ]
        mock_fast_server.api_raw_bytes = json.dumps(v1_payload).encode()
        v1_client, v1_targets = await client.get_targets(token="explicit")
        assert v1_client is None
        assert len(v1_targets) == 1
        assert v1_targets[0].url == mock_fast_server.target_url

        # Empty targets raises FastDotComConnectionError
        mock_fast_server.api_raw_bytes = json.dumps({"targets": []}).encode()
        with pytest.raises(FastDotComConnectionError, match="no valid speedtest"):
            await client.get_targets(token="explicit")

        # Missing targets list
        mock_fast_server.api_raw_bytes = json.dumps({"targets": "invalid"}).encode()
        with pytest.raises(FastDotComConnectionError, match="missing 'targets' list"):
            await client.get_targets(token="explicit")

        # Unexpected JSON root type
        mock_fast_server.api_raw_bytes = b"123"
        with pytest.raises(FastDotComConnectionError, match="Unexpected JSON"):
            await client.get_targets(token="explicit")

        # Non-200 status
        mock_fast_server.api_raw_bytes = None
        mock_fast_server.api_status = 502
        with pytest.raises(FastDotComConnectionError, match="HTTP 502"):
            await client.get_targets(token="explicit")
        mock_fast_server.api_status = 200

    async with FastDotComClient(
        timeout=0.05,
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as slow_client:
        mock_fast_server.api_delay = 0.2
        with pytest.raises(FastDotComTimeoutError, match="Timed out fetching"):
            await slow_client.get_targets(token="explicit")
        mock_fast_server.api_delay = 0.0

    async with FastDotComClient(
        fast_com_url=mock_fast_server.fast_com_url,
        api_url="http://127.0.0.1:1/v2",
    ) as bad_client:
        with pytest.raises(FastDotComConnectionError, match="Network error fetching"):
            await bad_client.get_targets(token="explicit")


async def test_measure_latency_and_errors(mock_fast_server: MockFastServer) -> None:
    """measure_latency runs warmup + probes and validates errors/parameters."""
    async with FastDotComClient() as client:
        with pytest.raises(ValueError, match="count must be at least 1"):
            await client.measure_latency(mock_fast_server.target_url, count=0)
        with pytest.raises(ValueError, match="probe_timeout must be positive"):
            await client.measure_latency(mock_fast_server.target_url, probe_timeout=0)

        avg_ms, samples = await client.measure_latency(
            mock_fast_server.target_url, count=3, warmup=True
        )
        assert avg_ms > 0
        assert len(samples) == 3
        range_gets = [
            req
            for req in mock_fast_server.requests
            if req == ("GET", "/speedtest/range/0-0")
        ]
        assert len(range_gets) == 4  # 1 warmup + 3 measured

        avg_no_warmup, samples_no_warmup = await client.measure_latency(
            mock_fast_server.target_url, count=2, warmup=False
        )
        assert avg_no_warmup > 0
        assert len(samples_no_warmup) == 2

        mock_fast_server.range_get_status = 500
        with pytest.raises(FastDotComConnectionError, match="HTTP 500"):
            await client.measure_latency(
                mock_fast_server.target_url, count=1, warmup=False
            )
        mock_fast_server.range_get_status = 200

        mock_fast_server.range_get_delay = 0.2
        with pytest.raises(FastDotComTimeoutError, match="latency probe timed out"):
            await client.measure_latency(
                mock_fast_server.target_url,
                count=1,
                warmup=False,
                probe_timeout=0.05,
            )
        mock_fast_server.range_get_delay = 0.0

        with pytest.raises(FastDotComConnectionError, match="latency probe failed"):
            await client.measure_latency(
                "http://127.0.0.1:1/speedtest", count=1, warmup=False
            )


async def test_measure_download_scaling_stability_and_errors(
    mock_fast_server: MockFastServer,
) -> None:
    """measure_download scales, records loaded latency, and cancels cleanly."""
    baseline_tasks = len(asyncio.all_tasks())
    mock_fast_server.range_get_fail_times = 1  # Transient probe error is tolerated
    mock_fast_server.download_chunk_delay = 0.005

    async with FastDotComClient() as client:
        mbps, total_bytes, avg_loaded, loaded_samples = await client.measure_download(
            [FastTarget(url=mock_fast_server.target_url)],
            min_time=0.06,
            max_time=0.35,
            connections_min=1,
            connections_max=4,
            sample_interval=0.03,
            ping_interval=0.03,
        )
        assert mbps > 0
        assert total_bytes > 0
        assert avg_loaded > 0
        assert len(loaded_samples) >= 1
        assert len(asyncio.all_tasks()) == baseline_tasks

        # Early stability stop when speed is steady across samples
        (
            mbps_no_lat,
            bytes_no_lat,
            loaded_zero,
            samples_zero,
        ) = await client.measure_download(
            [mock_fast_server.target_url],
            min_time=0.04,
            max_time=0.4,
            sample_interval=0.02,
            measure_loaded_latency=False,
        )
        assert mbps_no_lat > 0
        assert bytes_no_lat > 0
        assert loaded_zero == 0.0
        assert samples_zero == []

        # Server returning HTTP 500 fails cleanly instead of returning 0 Mbps
        mock_fast_server.download_status = 500
        with pytest.raises(FastDotComConnectionError, match="HTTP 500"):
            await client.measure_download(
                [mock_fast_server.target_url],
                min_time=0.02,
                max_time=0.1,
                sample_interval=0.02,
                measure_loaded_latency=False,
            )
        mock_fast_server.download_status = 200

        # Invalid parameters
        with pytest.raises(ValueError, match="non-empty sequence"):
            await client.measure_download([])
        with pytest.raises(ValueError, match="0 < min_time <= max_time"):
            await client.measure_download(
                [mock_fast_server.target_url], min_time=2.0, max_time=1.0
            )
        with pytest.raises(ValueError, match="1 <= connections_min <= connections_max"):
            await client.measure_download(
                [mock_fast_server.target_url], connections_min=4, connections_max=2
            )
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            await client.measure_download([mock_fast_server.target_url], chunk_size=0)
        with pytest.raises(ValueError, match="window_size must be at least 1"):
            await client.measure_download([mock_fast_server.target_url], window_size=0)
        with pytest.raises(ValueError, match="sample_interval must be positive"):
            await client.measure_download(
                [mock_fast_server.target_url], sample_interval=0
            )
        with pytest.raises(ValueError, match="ping_interval and probe_timeout"):
            await client.measure_download(
                [mock_fast_server.target_url], ping_interval=0
            )

    # Unreachable endpoint raises FastDotComConnectionError
    async with FastDotComClient(timeout=0.5) as bad_client:
        with pytest.raises(FastDotComConnectionError):
            await bad_client.measure_download(
                ["http://127.0.0.1:1/speedtest"],
                min_time=0.02,
                max_time=0.1,
                sample_interval=0.02,
                measure_loaded_latency=False,
            )


async def test_measure_upload_streaming_headers_and_errors(
    mock_fast_server: MockFastServer,
) -> None:
    """measure_upload sends Content-Length, handles remainders, and cleans up."""
    baseline_tasks = len(asyncio.all_tasks())

    async with FastDotComClient() as client:
        with pytest.raises(ValueError, match="payload_size must be positive"):
            await client.measure_upload([mock_fast_server.target_url], payload_size=0)

        # Non-multiple payload_size (150 KiB with 64 KiB chunk) verifies remainder slice
        mbps, total_bytes = await client.measure_upload(
            [FastTarget(url=mock_fast_server.target_url)],
            min_time=0.05,
            max_time=0.15,
            connections_min=1,
            connections_max=4,
            chunk_size=64 * 1024,
            payload_size=150 * 1024,
            sample_interval=0.02,
        )
        assert mbps > 0
        assert total_bytes > 0
        assert mock_fast_server.upload_bytes_received > 0
        assert len(mock_fast_server.upload_headers_seen) >= 1
        first_headers = mock_fast_server.upload_headers_seen[0]
        assert first_headers.get("Content-Length") == str(150 * 1024)
        assert first_headers.get("Content-Type") == "application/octet-stream"
        assert "chunked" not in first_headers.get("Transfer-Encoding", "").lower()
        assert len(asyncio.all_tasks()) == baseline_tasks

        # HTTP error rolls back bytes and raises FastDotComConnectionError
        mock_fast_server.upload_status = 500
        with pytest.raises(
            FastDotComConnectionError, match="Upload failed with HTTP 500"
        ):
            await client.measure_upload(
                [mock_fast_server.target_url],
                min_time=0.02,
                max_time=0.1,
                payload_size=32 * 1024,
                sample_interval=0.02,
            )
        mock_fast_server.upload_status = 200

    # Timeout during stalled transfer raises FastDotComTimeoutError
    mock_fast_server.download_chunk_delay = 0.2
    async with FastDotComClient(timeout=0.04) as timeout_client:
        with pytest.raises(FastDotComTimeoutError, match=r"(?i)timeout|timed out"):
            await timeout_client.measure_download(
                [mock_fast_server.target_url],
                min_time=0.05,
                max_time=0.25,
                sample_interval=0.02,
                measure_loaded_latency=False,
            )


async def test_run_speedtest_single_v2_call_and_selective_flags(
    mock_fast_server: MockFastServer,
) -> None:
    """run_speedtest makes a single v2 call and honors selective phase flags."""
    async with FastDotComClient(
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as client:
        result = await client.run_speedtest(
            min_time=0.05,
            max_time=0.12,
            ping_count=2,
            ping_interval=0.02,
            upload_payload_size=64 * 1024,
        )
        assert mock_fast_server.v2_calls == 1
        assert result.download_speed > 0
        assert result.upload_speed > 0
        assert result.unloaded_ping > 0
        assert result.loaded_ping >= 0
        assert result.client is not None
        assert result.client.location is not None
        assert result.client.location.city == "Málaga"
        assert result.to_dict() == {
            "download_speed": result.download_speed,
            "upload_speed": result.upload_speed,
            "unloaded_ping": result.unloaded_ping,
            "loaded_ping": result.loaded_ping,
            "success": 1,
        }

        selective = await client.run_speedtest(
            measure_download=False,
            measure_upload=False,
            measure_latency=False,
        )
        assert selective.download_speed == 0.0
        assert selective.upload_speed == 0.0
        assert selective.unloaded_ping == 0.0
        assert selective.loaded_ping == 0.0


async def test_async_fast_com_and_sync_fast_com(
    mock_fast_server: MockFastServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """async_fast_com and sync fast_com work both inside and outside event loops."""
    async with aiohttp.ClientSession() as session:
        res = await async_fast_com(
            session=session,
            min_time=0.05,
            max_time=0.1,
            ping_count=2,
            ping_interval=0.02,
            upload_payload_size=64 * 1024,
            fast_com_url=mock_fast_server.fast_com_url,
            api_url=mock_fast_server.api_url,
        )
        assert res.download_speed > 0
        assert not session.closed

    with pytest.raises(ValueError, match="payload_size must be positive"):
        fast_com(payload_size=0)
    with pytest.raises(ValueError, match="max_payload_size must be positive"):
        fast_com(max_payload_size=-1)

    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
        caplog.at_level(logging.INFO, logger="fastdotcom"),
    ):
        # Called directly inside a running loop -> exercises ThreadPoolExecutor path
        metrics_inside_loop = fast_com(
            verbose=True,
            max_time=0.1,
            ping_count=2,
            payload_size=128 * 1024,
            max_payload_size=64 * 1024,
        )
        assert metrics_inside_loop["success"] == 1
        assert metrics_inside_loop["download_speed"] > 0
        assert "Fast.com speedtest complete" in caplog.text


def test_sync_fast_com_outside_event_loop(mock_fast_server: MockFastServer) -> None:
    """Calling fast_com synchronously outside an event loop uses asyncio.run."""
    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
    ):
        result_a = fast_com(max_time=0.1, ping_count=2, payload_size=64 * 1024)
        result_b = fast_com(max_time=0.1, ping_count=2, max_payload_size=64 * 1024)
    assert result_a["success"] == 1
    assert result_b["success"] == 1
    assert result_a["download_speed"] > 0
    assert result_a["upload_speed"] > 0


async def test_client_edge_branches_for_coverage(
    mock_fast_server: MockFastServer,
) -> None:
    """Cover remaining edge cases in worker error normalization and stability exit."""
    from fastdotcom.client import _wrap_worker_error

    err = FastDotComConnectionError("direct")
    assert _wrap_worker_error(err) is err
    assert isinstance(_wrap_worker_error(TimeoutError("t")), FastDotComTimeoutError)
    assert isinstance(_wrap_worker_error(RuntimeError("r")), FastDotComConnectionError)

    # Zero-byte download without recorded exception when server sends empty 200 response
    mock_fast_server.download_chunks = 0
    async with FastDotComClient(
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as client:
        with pytest.raises(FastDotComConnectionError, match="0 bytes"):
            await client.measure_download(
                [mock_fast_server.target_url],
                min_time=0.02,
                max_time=0.08,
                sample_interval=0.02,
                measure_loaded_latency=False,
            )

    # Peak fallback when transfer finishes before second sample window closes
    mock_fast_server.download_chunks = 4
    mock_fast_server.download_chunk_size = 16 * 1024
    async with FastDotComClient(
        fast_com_url=mock_fast_server.fast_com_url,
        api_url=mock_fast_server.api_url,
    ) as client:
        # Force steady stability exit after min_time
        mbps, total, _, _ = await client.measure_download(
            [mock_fast_server.target_url],
            min_time=0.02,
            max_time=0.3,
            sample_interval=0.02,
            measure_loaded_latency=False,
        )
        assert mbps > 0
        assert total > 0


def test_parse_helpers_optional_fields() -> None:
    """_parse_client_info and _parse_location handle missing or non-dict values."""
    from fastdotcom.client import _parse_client_info, _parse_location

    assert _parse_location("not-a-dict") is None
    assert _parse_client_info("not-a-dict") is None
    info = _parse_client_info({"ip": "1.2.3.4"})
    assert info is not None
    assert info.ip == "1.2.3.4"
    assert info.asn is None
    assert info.location is None


def test_misc_edge_branches(mock_fast_server: MockFastServer) -> None:
    """Cover remaining subtle branches in client orchestration."""

    async def _run() -> None:
        # Non-empty transfer finishing within tiny max_time < sample_interval
        async with FastDotComClient() as client:
            mbps, total = await client.measure_upload(
                [mock_fast_server.target_url],
                min_time=0.005,
                max_time=0.01,
                payload_size=32 * 1024,
                sample_interval=0.5,
            )
            assert mbps > 0
            assert total > 0

        # Non-FastDotComError exception from aiohttp inside worker
        async with FastDotComClient() as bad_client:
            with pytest.raises(FastDotComConnectionError):
                await bad_client.measure_upload(
                    ["http://127.0.0.1:1/speedtest"],
                    min_time=0.02,
                    max_time=0.1,
                    payload_size=16 * 1024,
                    sample_interval=0.02,
                )

    asyncio.run(_run())


def test_orchestrate_peak_fallback(mock_fast_server: MockFastServer) -> None:
    """Verify peak_mbps fallback when sample window reports 0 dt."""
    import itertools

    async def _run() -> None:
        total = 0

        async def _worker(_url: str) -> None:
            nonlocal total
            total = 1024

        stop_event = asyncio.Event()
        async with FastDotComClient() as client:
            with patch(
                "fastdotcom.client.time.perf_counter",
                side_effect=itertools.chain([1.0, 1.0, 1.0], itertools.repeat(2.0)),
            ):
                mbps, got = await client._orchestrate_transfer(
                    [mock_fast_server.target_url],
                    make_worker=_worker,
                    get_bytes=lambda: total,
                    get_error=lambda: None,
                    stop_event=stop_event,
                    min_time=0.01,
                    max_time=0.02,
                    connections_min=1,
                    connections_max=1,
                    window_size=2,
                    sample_interval=0.01,
                )
        assert got == 1024
        assert mbps > 0

    asyncio.run(_run())


async def test_fast_tcp_connector_aborts_ssl_on_forced_release() -> None:
    """_FastTCPConnector cancels drain waiters and aborts SSL on forced release."""
    loop = asyncio.get_running_loop()
    connector = _FastTCPConnector()
    try:
        with patch.object(aiohttp.TCPConnector, "_release") as super_release:
            ssl_key = Mock(is_ssl=True)
            plain_key = Mock(is_ssl=False)

            waiter_a: asyncio.Future[None] = loop.create_future()
            transport_a = Mock()
            proto_a = Mock(transport=transport_a, _drain_waiter=waiter_a)
            connector._release(ssl_key, proto_a, should_close=True)
            assert waiter_a.cancelled()
            transport_a.abort.assert_called_once()

            waiter_b: asyncio.Future[None] = loop.create_future()
            waiter_b.set_result(None)
            transport_b = Mock()
            proto_b = Mock(transport=transport_b, _drain_waiter=waiter_b)
            connector._release(plain_key, proto_b, should_close=True)
            assert not waiter_b.cancelled()
            transport_b.abort.assert_not_called()

            proto_c = Mock(transport=None, _drain_waiter=None)
            connector._release(ssl_key, proto_c, should_close=True)

            waiter_d: asyncio.Future[None] = loop.create_future()
            transport_d = Mock()
            proto_d = Mock(transport=transport_d, _drain_waiter=waiter_d)
            connector._release(ssl_key, proto_d, should_close=False)
            assert not waiter_d.cancelled()
            transport_d.abort.assert_not_called()
            waiter_d.cancel()

            assert super_release.call_count == 4
    finally:
        await connector.close()
