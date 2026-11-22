"""Immutable data models for fastdotcom."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FastLocation:
    """Geographic location metadata returned by Fast.com API v2."""

    city: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class FastClientInfo:
    """Client network metadata returned by Fast.com API v2."""

    ip: str | None = None
    asn: str | None = None
    isp: str | None = None
    location: FastLocation | None = None


@dataclass(frozen=True, slots=True)
class FastTarget:
    """Fast.com speedtest server target."""

    url: str
    name: str | None = None
    location: FastLocation | None = None


@dataclass(frozen=True, slots=True)
class FastSpeedtestResult:
    """Complete result of a Fast.com speedtest run."""

    download_speed: float = 0.0
    upload_speed: float = 0.0
    unloaded_ping: float = 0.0
    loaded_ping: float = 0.0
    unloaded_pings: tuple[float, ...] = ()
    loaded_pings: tuple[float, ...] = ()
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    client: FastClientInfo | None = None
    targets: tuple[FastTarget, ...] = ()
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """Return Home Assistant drop-in compatible metric dictionary."""
        return {
            "download_speed": self.download_speed,
            "upload_speed": self.upload_speed,
            "unloaded_ping": self.unloaded_ping,
            "loaded_ping": self.loaded_ping,
            "success": 1,
        }
