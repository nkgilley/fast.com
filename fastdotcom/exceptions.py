"""Typed exceptions for fastdotcom."""

from __future__ import annotations


class FastDotComError(Exception):
    """Base exception for all Fast.com speedtest errors."""


class FastDotComConnectionError(FastDotComError):
    """Raised when communicating with Fast.com or OCA endpoints fails."""


class FastDotComTokenError(FastDotComError):
    """Raised when extracting the Fast.com public token fails."""


class FastDotComTimeoutError(FastDotComError):
    """Raised when a Fast.com HTTP request or probe times out."""
