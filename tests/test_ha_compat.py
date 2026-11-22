"""Home Assistant integration compatibility contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import aiohttp
import pytest

import fastdotcom
from fastdotcom import FastDotComError, async_fast_com, fast_com

if TYPE_CHECKING:
    from .conftest import MockFastServer


async def test_ha_executor_job_contract(mock_fast_server: MockFastServer) -> None:
    """Simulate hass.async_add_executor_job(fast_com) and HA diagnostics."""
    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
    ):
        data = await asyncio.to_thread(fast_com, max_time=0.1, ping_count=2)

    assert set(data) == {
        "download_speed",
        "upload_speed",
        "unloaded_ping",
        "loaded_ping",
        "success",
    }
    assert isinstance(data["download_speed"], float)
    assert isinstance(data["upload_speed"], float)
    assert isinstance(data["unloaded_ping"], float)
    assert isinstance(data["loaded_ping"], float)
    assert data["success"] == 1
    assert data["download_speed"] > 0
    assert data["upload_speed"] > 0
    assert json.loads(json.dumps({"coordinator_data": data})) == {
        "coordinator_data": data
    }


async def test_ha_coordinator_raises_on_failure_instead_of_zero(
    mock_fast_server: MockFastServer,
) -> None:
    """Network failures raise FastDotComError so HA marks entity unavailable."""
    mock_fast_server.api_status = 503
    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
        pytest.raises(FastDotComError),
    ):
        await asyncio.to_thread(fast_com, max_time=0.1, ping_count=2)


async def test_ha_injected_websession_never_closed(
    mock_fast_server: MockFastServer,
) -> None:
    """async_fast_com(session=...) mirrors HA async_get_clientsession ownership."""
    async with aiohttp.ClientSession() as ha_session:
        result = await async_fast_com(
            session=ha_session,
            min_time=0.05,
            max_time=0.1,
            ping_count=2,
            ping_interval=0.02,
            upload_payload_size=64 * 1024,
            fast_com_url=mock_fast_server.fast_com_url,
            api_url=mock_fast_server.api_url,
        )
        assert result.download_speed > 0
        assert not ha_session.closed


async def test_ha_logger_namespace_propagation(
    mock_fast_server: MockFastServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All library logs propagate to HA's manifest.json logger 'fastdotcom'."""
    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
        caplog.at_level(logging.DEBUG, logger="fastdotcom"),
    ):
        await async_fast_com(
            min_time=0.05,
            max_time=0.1,
            ping_count=2,
            ping_interval=0.02,
            upload_payload_size=64 * 1024,
            fast_com_url=mock_fast_server.fast_com_url,
            api_url=mock_fast_server.api_url,
        )

    assert any(record.name.startswith("fastdotcom") for record in caplog.records)
