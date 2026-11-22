"""Tests for the fastdotcom CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import fastdotcom
from fastdotcom import FastClientInfo, FastLocation, FastSpeedtestResult
from fastdotcom.cli import format_result, main

if TYPE_CHECKING:
    import pytest

    from .conftest import MockFastServer


def test_format_result_client_variants() -> None:
    """format_result handles full, partial, and absent client metadata cleanly."""
    full = FastSpeedtestResult(
        download_speed=120.5,
        upload_speed=60.25,
        unloaded_ping=4.1,
        loaded_ping=12.3,
        elapsed_seconds=5.5,
        client=FastClientInfo(
            isp="FiberCo",
            location=FastLocation(city="Madrid", country="ES"),
        ),
    )
    rendered_full = format_result(full)
    assert "120.50 Mbps" in rendered_full
    assert "FiberCo — Madrid, ES" in rendered_full

    partial = FastSpeedtestResult(
        download_speed=10.0,
        client=FastClientInfo(isp=None, location=FastLocation()),
    )
    assert "Client:" not in format_result(partial)

    no_client = FastSpeedtestResult(download_speed=10.0)
    assert "Client:" not in format_result(no_client)


def test_cli_human_and_json_output(
    mock_fast_server: MockFastServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI outputs human summary and valid JSON against the mock server."""
    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
    ):
        rc_human = main(["--max-time", "0.1", "--ping-count", "2", "--verbose"])
        out_human = capsys.readouterr().out
        assert rc_human == 0
        assert "Download:" in out_human
        assert "Example Fiber — Málaga, ES" in out_human

        rc_json = main(
            [
                "--max-time",
                "0.1",
                "--min-time",
                "0.05",
                "--ping-count",
                "2",
                "--no-upload",
                "--no-latency",
                "--json",
            ]
        )
        out_json = capsys.readouterr().out
        assert rc_json == 0
        parsed = json.loads(out_json)
        assert parsed["download_speed"] > 0
        assert parsed["upload_speed"] == 0.0
        assert parsed["unloaded_ping"] == 0.0


def test_cli_errors_exit_nonzero(
    mock_fast_server: MockFastServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI reports FastDotComError and ValueError cleanly on stderr with exit code 1."""
    rc_bad_flags = main(["--ipv4", "--ipv6"])
    err_flags = capsys.readouterr().err
    assert rc_bad_flags == 1
    assert "both IPv4 and IPv6" in err_flags

    mock_fast_server.api_status = 500
    with (
        patch.object(fastdotcom, "FAST_COM_URL", mock_fast_server.fast_com_url),
        patch.object(fastdotcom, "FAST_API_V2_URL", mock_fast_server.api_url),
    ):
        rc_api_err = main(["--max-time", "0.1", "--ping-count", "1"])
    err_api = capsys.readouterr().err
    assert rc_api_err == 1
    assert "Error:" in err_api
