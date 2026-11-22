"""Minimal-bandwidth live Fast.com protocol smoke tests (deselected by default)."""

from __future__ import annotations

import pytest

from fastdotcom import FastDotComClient, to_range_url


@pytest.mark.real_api_call
async def test_real_fast_com_protocol_smoke() -> None:
    """Verify live Fast.com protocol compatibility using minimal (<1 MiB) traffic."""
    async with FastDotComClient() as client:
        token = await client.get_token()
        assert len(token) > 0

        _, targets = await client.get_targets(token, url_count=1)
        assert len(targets) >= 1
        assert targets[0].url.startswith("https://")

        unloaded_ms, unloaded_samples = await client.measure_latency(
            targets[0].url,
            count=2,
            warmup=True,
        )
        assert unloaded_ms > 0
        assert len(unloaded_samples) == 2

        tiny_download_url = to_range_url(targets[0].url, 0, 4095)
        dl_mbps, dl_bytes, loaded_ms, loaded_samples = await client.measure_download(
            [tiny_download_url],
            min_time=0.1,
            max_time=0.3,
            connections_min=1,
            connections_max=1,
            chunk_size=4096,
            sample_interval=0.05,
            measure_loaded_latency=True,
            ping_interval=0.05,
        )
        assert dl_mbps > 0
        assert 0 < dl_bytes < 2 * 1024 * 1024
        assert loaded_ms > 0
        assert len(loaded_samples) >= 1

        ul_mbps, ul_bytes = await client.measure_upload(
            [targets[0]],
            min_time=0.1,
            max_time=0.3,
            connections_min=1,
            connections_max=1,
            chunk_size=4096,
            payload_size=4096,
            sample_interval=0.05,
        )
        assert ul_mbps > 0
        assert 0 < ul_bytes < 2 * 1024 * 1024
