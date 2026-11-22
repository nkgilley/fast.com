"""Command-line interface for fastdotcom."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from typing import TYPE_CHECKING

from . import __version__, async_fast_com
from .const import DEFAULT_MAX_TIME, DEFAULT_MIN_TIME, DEFAULT_PING_COUNT
from .exceptions import FastDotComError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import FastSpeedtestResult


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="fastdotcom",
        description="Measure Internet speed using Netflix's Fast.com API v2.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--max-time",
        type=float,
        default=DEFAULT_MAX_TIME,
        help="Maximum duration in seconds for download/upload phases.",
    )
    parser.add_argument(
        "--min-time",
        type=float,
        default=None,
        help="Minimum duration in seconds before early stability stop.",
    )
    parser.add_argument(
        "--ping-count",
        type=int,
        default=DEFAULT_PING_COUNT,
        help="Number of HTTP latency probes to measure.",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Skip the download speed test.",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip the upload speed test.",
    )
    parser.add_argument(
        "--no-latency",
        action="store_true",
        help="Skip unloaded and loaded HTTP latency measurements.",
    )
    parser.add_argument(
        "--ipv4",
        action="store_true",
        help="Force IPv4 connections.",
    )
    parser.add_argument(
        "--ipv6",
        action="store_true",
        help="Force IPv6 connections.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    return parser


def format_result(result: FastSpeedtestResult) -> str:
    """Format a FastSpeedtestResult for human-readable terminal display."""
    lines = [
        f"Download:        {result.download_speed:.2f} Mbps",
        f"Upload:          {result.upload_speed:.2f} Mbps",
        f"Unloaded Ping:   {result.unloaded_ping:.2f} ms",
        f"Loaded Ping:     {result.loaded_ping:.2f} ms",
        f"Elapsed:         {result.elapsed_seconds:.2f} s",
    ]
    if result.client is not None:
        loc_parts = []
        if result.client.location is not None:
            if result.client.location.city:
                loc_parts.append(result.client.location.city)
            if result.client.location.country:
                loc_parts.append(result.client.location.country)
        loc_str = ", ".join(loc_parts)
        client_bits = [bit for bit in (result.client.isp, loc_str) if bit]
        if client_bits:
            lines.append(f"Client:          {' — '.join(client_bits)}")
    return "\n".join(lines)


def result_to_json(result: FastSpeedtestResult) -> str:
    """Serialize a FastSpeedtestResult as formatted JSON."""
    payload = asdict(result)
    return json.dumps(payload, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning an exit status code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
        )

    effective_min_time = (
        args.min_time
        if args.min_time is not None
        else min(DEFAULT_MIN_TIME, args.max_time)
    )

    try:
        result = asyncio.run(
            async_fast_com(
                min_time=effective_min_time,
                max_time=args.max_time,
                ping_count=args.ping_count,
                measure_download=not args.no_download,
                measure_upload=not args.no_upload,
                measure_latency=not args.no_latency,
                force_ipv4=args.ipv4,
                force_ipv6=args.ipv6,
            )
        )
    except (FastDotComError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(result_to_json(result))
    else:
        print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
