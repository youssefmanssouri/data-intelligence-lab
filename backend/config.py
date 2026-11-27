"""
Data Intelligence Lab - Backend Configuration
Defines configuration settings and authoritative host validation for the local API gateway.
Local mode defaults strictly to HOST = 127.0.0.1.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("backend.config")

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8000
DEFAULT_MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024  # 200 MB
MAX_CONFIGURED_UPLOAD_BYTES: int = sys.maxsize

APP_DIR_NAME: str = "Data Intelligence Lab"
LEGACY_DIR_NAME: str = ".data-intelligence-lab"
STORAGE_SUBDIR: str = "storage"


def validate_port(port: object) -> int:
    """
    Validates a TCP port number.
    Must be a plain integer in [0, 65535].
    Port 0 is explicitly supported for dynamic loopback allocation.
    """
    if type(port) is not int:
        raise TypeError("Port must be a plain integer.")
    if not 0 <= port <= 65535:
        raise ValueError(f"Port must be between 0 and 65535, got {port!r}.")
    return port


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


@dataclass(frozen=True)
class LegacyStorageInfo:
    """Diagnostic state regarding legacy storage and migration status."""
    detected: bool
    status: str
    legacy_dir: Path
    legacy_count: int
    active_count: int
    warning: Optional[str]
    incomplete_count: int = 0
    error: Optional[str] = None


def get_legacy_storage_dir() -> Path:
    """Returns the legacy storage directory path (~/.data-intelligence-lab/storage)."""
    return (Path.home() / LEGACY_DIR_NAME / STORAGE_SUBDIR).resolve()


def _is_valid_localappdata(value: Optional[str]) -> bool:
    """
    Validates whether the provided LOCALAPPDATA path string is acceptable.
    Rejects missing, empty, whitespace-only, relative, or filesystem root paths.
    """
    if value is None or not isinstance(value, str):
        return False
    cleaned = value.strip()
    if not cleaned or "\0" in cleaned:
        return False
    try:
        p = Path(cleaned)
        # On Windows, a valid LOCALAPPDATA path must be absolute
        if not p.is_absolute():
            return False
        resolved = p.resolve()
        # Must not resolve to a filesystem root or anchor (e.g. C:\ or \)
        if resolved == resolved.parent or str(resolved).rstrip("\\/") == resolved.anchor.rstrip("\\/"):
            return False
        # If it exists on disk, it must be a directory, not a file
        if resolved.exists() and not resolved.is_dir():
            return False
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def resolve_default_storage_dir(platform_name: Optional[str] = None) -> Path:
    """
    Resolves the authoritative default storage directory based on operating system.

    For Data Intelligence Lab v1.0, default storage on all platforms resolves to:
      Path.home() / ".data-intelligence-lab" / "storage"
    This guarantees zero data loss, full backward compatibility with pre-existing
    user datasets, and eliminates filesystem-dependent storage fragmentation.

    Does NOT create any directories on the filesystem.
    """
    return get_legacy_storage_dir()


def scan_dataset_directories(storage_dir: Path) -> tuple[int, int, Optional[str]]:
    """
    Scans storage_dir/datasets and returns (valid_count, incomplete_count, error).

    A directory under datasets/ is:
    - Ignored if its name starts with '.' (staging directory).
    - Valid if it is a directory and contains both 'data.parquet' and 'metadata.json' as files.
    - Incomplete if it is a directory but lacks 'data.parquet', lacks 'metadata.json',
      or an individual entry cannot be inspected due to OSError.

    If storage_dir or storage_dir/datasets does not exist:
    returns (0, 0, None).

    If an inaccessible directory or filesystem error prevents inspection:
    returns (valid_count, incomplete_count, error_name).
    """
    try:
        if not storage_dir.exists():
            return 0, 0, None
    except (OSError, PermissionError) as exc:
        return 0, 0, type(exc).__name__

    datasets_dir = storage_dir / "datasets"
    try:
        if not datasets_dir.exists() or not datasets_dir.is_dir():
            return 0, 0, None
    except (OSError, PermissionError) as exc:
        return 0, 0, type(exc).__name__

    valid_count = 0
    incomplete_count = 0

    try:
        for entry in datasets_dir.iterdir():
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    has_data = (entry / "data.parquet").is_file()
                    has_meta = (entry / "metadata.json").is_file()
                    if has_data and has_meta:
                        valid_count += 1
                    else:
                        incomplete_count += 1
            except (OSError, PermissionError):
                incomplete_count += 1
    except (OSError, PermissionError) as exc:
        return valid_count, incomplete_count, type(exc).__name__

    return valid_count, incomplete_count, None


def count_valid_datasets(storage_dir: Path) -> int:
    """
    Counts valid dataset directories under storage_dir/datasets.
    A valid dataset directory contains both data.parquet and metadata.json
    and does not start with a dot (staging directory).
    Fails closed with 0 on any filesystem error or missing directory.
    Does NOT create any directories.
    """
    valid_count, _, _ = scan_dataset_directories(storage_dir)
    return valid_count


def inspect_legacy_storage(active_storage_dir: Path, is_explicit: bool = False) -> LegacyStorageInfo:
    """
    Inspects legacy storage state and returns diagnostic status, counts, and warnings.

    Status precedence:
    1. ACTIVE_IS_LEGACY: active_storage_dir is identical to legacy_dir.
    2. EXPLICIT_OVERRIDE: active_storage_dir was explicitly specified by caller or STORAGE_DIR env.
    3. LEGACY_INSPECTION_FAILED: legacy directory exists but could not be inspected (PermissionError/OSError).
    4. DUAL_STORAGE_DETECTED: both legacy and active storage directories contain >= 1 valid datasets.
    5. LEGACY_DATA_DETECTED: legacy directory contains >= 1 valid datasets, active storage has 0 valid.
    6. INCOMPLETE_LEGACY_DATA: legacy directory contains >= 1 incomplete artifacts, 0 valid datasets.
    7. NO_LEGACY_DATA: legacy directory does not exist or has 0 entries, with no inspection errors.

    Deterministic Rule:
    The active storage directory (active_storage_dir) remains the authoritative target.
    Existing legacy data is never automatically moved, copied, merged, repaired, or deleted.
    """
    legacy_dir = get_legacy_storage_dir()
    resolved_active = active_storage_dir.resolve()
    resolved_legacy = legacy_dir.resolve()

    # 1. If active storage IS legacy storage (e.g. non-Windows default or explicit legacy)
    if resolved_active == resolved_legacy:
        legacy_valid, legacy_incomplete, legacy_err = scan_dataset_directories(resolved_legacy)
        return LegacyStorageInfo(
            detected=(legacy_valid > 0 or legacy_incomplete > 0 or legacy_err is not None),
            status="ACTIVE_IS_LEGACY",
            legacy_dir=resolved_legacy,
            legacy_count=legacy_valid,
            incomplete_count=legacy_incomplete,
            active_count=legacy_valid,
            error=legacy_err,
            warning=None,
        )

    # 2. Scan legacy and active storage directories
    legacy_valid, legacy_incomplete, legacy_err = scan_dataset_directories(resolved_legacy)
    active_valid, active_incomplete, _ = scan_dataset_directories(resolved_active)

    # 3. Explicit override takes precedence in status, but records any legacy findings
    if is_explicit:
        warning = None
        if legacy_err is not None:
            warning = (
                f"Explicit storage directory '{resolved_active}' is active. "
                f"Legacy dataset storage at '{resolved_legacy}' could not be inspected ({legacy_err})."
            )
        elif legacy_valid > 0 or legacy_incomplete > 0:
            details = []
            if legacy_valid > 0:
                details.append(f"{legacy_valid} valid dataset(s)")
            if legacy_incomplete > 0:
                details.append(f"{legacy_incomplete} incomplete artifact(s)")
            warning = (
                f"Explicit storage directory '{resolved_active}' is active. "
                f"Legacy dataset storage at '{resolved_legacy}' contains {', '.join(details)} "
                f"which were not moved or altered."
            )
        return LegacyStorageInfo(
            detected=(legacy_valid > 0 or legacy_incomplete > 0 or legacy_err is not None),
            status="EXPLICIT_OVERRIDE",
            legacy_dir=resolved_legacy,
            legacy_count=legacy_valid,
            incomplete_count=legacy_incomplete,
            active_count=active_valid,
            error=legacy_err,
            warning=warning,
        )

    # 4. If legacy inspection failed (PermissionError / OSError)
    if legacy_err is not None:
        warning = (
            f"Legacy storage exists at '{resolved_legacy}' but could not be inspected "
            f"due to a filesystem error ({legacy_err}). Existing legacy data was not modified."
        )
        return LegacyStorageInfo(
            detected=True,
            status="LEGACY_INSPECTION_FAILED",
            legacy_dir=resolved_legacy,
            legacy_count=legacy_valid,
            incomplete_count=legacy_incomplete,
            active_count=active_valid,
            error=legacy_err,
            warning=warning,
        )

    # 5. Dual storage: both legacy and active have valid datasets
    if legacy_valid > 0 and active_valid > 0:
        details = [f"{legacy_valid} dataset(s)"]
        if legacy_incomplete > 0:
            details.append(f"{legacy_incomplete} incomplete artifact(s)")
        warning = (
            f"Datasets exist in both legacy storage '{resolved_legacy}' ({', '.join(details)}) "
            f"and active storage '{resolved_active}' ({active_valid} dataset(s)). "
            f"Storage directories are not automatically merged."
        )
        return LegacyStorageInfo(
            detected=True,
            status="DUAL_STORAGE_DETECTED",
            legacy_dir=resolved_legacy,
            legacy_count=legacy_valid,
            incomplete_count=legacy_incomplete,
            active_count=active_valid,
            error=None,
            warning=warning,
        )

    # 6. Valid legacy datasets detected (active has 0 valid datasets)
    if legacy_valid > 0:
        details = [f"{legacy_valid} dataset(s)"]
        if legacy_incomplete > 0:
            details.append(f"{legacy_incomplete} incomplete artifact(s)")
        warning = (
            f"Legacy dataset storage detected at '{resolved_legacy}' ({', '.join(details)}). "
            f"Active storage is '{resolved_active}'. Existing datasets were not moved or modified."
        )
        return LegacyStorageInfo(
            detected=True,
            status="LEGACY_DATA_DETECTED",
            legacy_dir=resolved_legacy,
            legacy_count=legacy_valid,
            incomplete_count=legacy_incomplete,
            active_count=active_valid,
            error=None,
            warning=warning,
        )

    # 7. Incomplete legacy artifacts detected (0 valid legacy datasets, but >= 1 incomplete folders)
    if legacy_incomplete > 0:
        warning = (
            f"Incomplete legacy dataset artifacts detected at '{resolved_legacy}' "
            f"({legacy_incomplete} incomplete artifact(s)). "
            f"Active storage is '{resolved_active}'. Incomplete artifacts were not modified or repaired."
        )
        return LegacyStorageInfo(
            detected=True,
            status="INCOMPLETE_LEGACY_DATA",
            legacy_dir=resolved_legacy,
            legacy_count=0,
            incomplete_count=legacy_incomplete,
            active_count=active_valid,
            error=None,
            warning=warning,
        )

    # 8. No legacy data (legacy absent or empty, 0 valid, 0 incomplete, no errors)
    return LegacyStorageInfo(
        detected=False,
        status="NO_LEGACY_DATA",
        legacy_dir=resolved_legacy,
        legacy_count=0,
        incomplete_count=0,
        active_count=active_valid,
        error=None,
        warning=None,
    )


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

        port_env = os.environ.get("PORT")
        if port is not None:
            self.port = validate_port(port)
        elif port_env is not None and port_env.strip():
            cleaned_port = port_env.strip()
            if not cleaned_port.isascii() or not cleaned_port.isdecimal():
                raise ValueError("PORT environment variable must contain ASCII decimal digits.")
            self.port = validate_port(int(cleaned_port))
        else:
            self.port = DEFAULT_PORT

        env_storage = os.environ.get("STORAGE_DIR")
        if storage_dir is not None and str(storage_dir).strip():
            self.storage_dir = Path(os.path.expanduser(str(storage_dir).strip())).resolve()
            self.storage_is_explicit = True
        elif env_storage is not None and env_storage.strip():
            self.storage_dir = Path(os.path.expanduser(env_storage.strip())).resolve()
            self.storage_is_explicit = True
        else:
            self.storage_dir = resolve_default_storage_dir()
            self.storage_is_explicit = False

        self.legacy_storage_dir = get_legacy_storage_dir()
        legacy_info = inspect_legacy_storage(self.storage_dir, is_explicit=self.storage_is_explicit)
        self.legacy_storage_detected = legacy_info.detected
        self.legacy_storage_status = legacy_info.status
        self.legacy_datasets_count = legacy_info.legacy_count
        self.legacy_incomplete_count = legacy_info.incomplete_count
        self.legacy_error = legacy_info.error
        self.active_datasets_count = legacy_info.active_count
        self.legacy_warning = legacy_info.warning

        if self.legacy_warning:
            logger.warning(self.legacy_warning)

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
