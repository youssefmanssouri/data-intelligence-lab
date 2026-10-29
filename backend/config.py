"""
Data Intelligence Lab - Backend Configuration
Defines configuration settings and authoritative host validation for the local API gateway.
Local mode defaults strictly to HOST = 127.0.0.1.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path
from typing import Optional

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8000
DEFAULT_MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024  # 200 MB
MAX_CONFIGURED_UPLOAD_BYTES: int = sys.maxsize


def validate_upload_cap(value: object) -> int:
    """Require a plain integer byte count in [1, sys.maxsize]."""
    if type(value) is not int:
        raise TypeError("Upload byte limits must be plain integers.")
    if not 1 <= value <= MAX_CONFIGURED_UPLOAD_BYTES:
        raise ValueError("Upload byte limits must be between 1 and sys.maxsize.")
    return value


def _parse_upload_cap_environment(value: str) -> int:
    """Environment values use ASCII decimal digits; no signs or whitespace."""
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("MAX_UPLOAD_BYTES must contain ASCII decimal digits.")
    digits = value.lstrip("0") or "0"
    if len(digits) > len(str(MAX_CONFIGURED_UPLOAD_BYTES)):
        raise ValueError("MAX_UPLOAD_BYTES exceeds the supported limit.")
    return validate_upload_cap(int(digits))


def validate_host(host: Optional[str]) -> str:
    """
    Authoritative loopback host validation policy.
    Permits only explicit loopback IP address literals (default '127.0.0.1', '::1', 127.0.0.0/8).
    Strictly rejects:
    - Wildcard addresses (0.0.0.0, ::, 0)
    - Hostnames (e.g. 'localhost', 'example.com') that could resolve to non-loopback interfaces
    - Private / LAN addresses (e.g. 192.168.x.x, 10.x.x.x, 172.16.x.x)
    - Public Internet IP addresses (e.g. 8.8.8.8)
    - Values containing leading, trailing, or internal whitespace
    - Port-appended strings or malformed IP representations
    """
    if host is None or not isinstance(host, str):
        raise ValueError("Host must be a non-empty string.")

    if not host or host != host.strip() or any(c.isspace() for c in host):
        raise ValueError(f"Invalid host: whitespace or empty host value is prohibited: {host!r}")

    # Reject URLs, schemes, and host:port combinations
    if "://" in host or (":" in host and not host.startswith("[") and host.count(":") == 1):
        raise ValueError(f"Invalid host format: {host!r}. Port numbers or URL schemes must not be included.")

    # Strip optional IPv6 bracket notation, e.g. [::1] -> ::1
    cleaned = host[1:-1] if (host.startswith("[") and host.endswith("]")) else host

    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError:
        raise ValueError(
            f"Binding to host {host!r} is prohibited. "
            "Data Intelligence Lab local mode requires an explicit loopback IP address literal "
            "(e.g. '127.0.0.1' or '::1'). Hostnames and unparseable values are rejected."
        ) from None

    if not addr.is_loopback:
        raise ValueError(
            f"Binding to non-loopback host {host!r} is prohibited in local mode. "
            "Wildcards (0.0.0.0, ::), LAN addresses, and public IP addresses are strictly rejected."
        )

    return str(addr)


class Settings:
    """Application configuration settings."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        storage_dir: Optional[Path] = None,
        max_upload_bytes: Optional[int] = None,
        environment: Optional[str] = None,
    ) -> None:
        raw_host = host if host is not None else os.environ.get("HOST", DEFAULT_HOST)
        self.host = validate_host(raw_host)

        port_str = os.environ.get("PORT", str(DEFAULT_PORT))
        self.port = port if port is not None else int(port_str)

        env_storage = os.environ.get("STORAGE_DIR")
        if storage_dir is not None:
            self.storage_dir = Path(storage_dir).resolve()
        elif env_storage:
            self.storage_dir = Path(os.path.expanduser(env_storage)).resolve()
        else:
            self.storage_dir = (Path.home() / ".data-intelligence-lab" / "storage").resolve()

        max_upload_env = os.environ.get("MAX_UPLOAD_BYTES")
        if max_upload_bytes is not None:
            self.max_upload_bytes = validate_upload_cap(max_upload_bytes)
        elif max_upload_env is not None:
            self.max_upload_bytes = _parse_upload_cap_environment(max_upload_env)
        else:
            self.max_upload_bytes = DEFAULT_MAX_UPLOAD_BYTES

        self.environment = environment or os.environ.get("ENVIRONMENT", "development")


def get_settings() -> Settings:
    """Returns application configuration settings."""
    return Settings()
