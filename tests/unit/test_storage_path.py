"""
Data Intelligence Lab - Storage Path Resolution and Backward Compatibility Tests
Verifies default storage resolution to Path.home() / ".data-intelligence-lab" / "storage",
explicit override handling, empty/whitespace fallbacks, lazy directory creation,
missing directory tolerance, legacy storage detection, and accessibility of existing datasets.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from backend import config
from backend.adapters.local_storage import LocalDatasetStorage


def test_default_storage_resolves_to_legacy_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that default storage on all platforms resolves to ~/.data-intelligence-lab/storage."""
    monkeypatch.delenv("STORAGE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Mock\\LocalAppData")

    expected = (Path.home() / ".data-intelligence-lab" / "storage").resolve()

    assert config.resolve_default_storage_dir() == expected
    assert config.resolve_default_storage_dir(platform_name="nt") == expected
    assert config.resolve_default_storage_dir(platform_name="posix") == expected
    assert config.resolve_default_storage_dir(platform_name="darwin") == expected


def test_explicit_storage_dir_parameter_overrides_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that explicit storage_dir parameter overrides both defaults and STORAGE_DIR env."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "EnvStorage"))

    explicit_path = tmp_path / "ExplicitStorage"
    settings = config.Settings(storage_dir=explicit_path)

    assert settings.storage_dir == explicit_path.resolve()
    assert settings.storage_is_explicit is True


def test_explicit_storage_dir_environment_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that STORAGE_DIR environment variable overrides default resolution."""
    env_path = tmp_path / "EnvStorage"
    monkeypatch.setenv("STORAGE_DIR", str(env_path))

    settings = config.Settings()

    assert settings.storage_dir == env_path.resolve()
    assert settings.storage_is_explicit is True


@pytest.mark.parametrize("empty_value", ["", "   ", "\t  \n"])
def test_whitespace_or_empty_storage_dir_env_uses_default(empty_value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that empty or whitespace STORAGE_DIR env is treated as unset and uses default."""
    monkeypatch.setenv("STORAGE_DIR", empty_value)

    settings = config.Settings()
    expected = config.resolve_default_storage_dir()

    assert settings.storage_dir == expected
    assert settings.storage_is_explicit is False


@pytest.mark.parametrize("empty_value", ["", "   ", "\t  \n"])
def test_whitespace_or_empty_storage_dir_param_uses_default(empty_value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that empty or whitespace storage_dir parameter is treated as unset and uses default."""
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    settings = config.Settings(storage_dir=empty_value)  # type: ignore[arg-type]
    expected = config.resolve_default_storage_dir()

    assert settings.storage_dir == expected
    assert settings.storage_is_explicit is False


def test_tilde_expansion_in_storage_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that ~ is properly expanded in storage_dir parameter and environment variable."""
    monkeypatch.delenv("STORAGE_DIR", raising=False)
    settings = config.Settings(storage_dir="~/.test-data-storage")
    assert settings.storage_dir == (Path.home() / ".test-data-storage").resolve()
    assert settings.storage_is_explicit is True

    monkeypatch.setenv("STORAGE_DIR", "~/.test-env-storage")
    settings_env = config.Settings()
    assert settings_env.storage_dir == (Path.home() / ".test-env-storage").resolve()
    assert settings_env.storage_is_explicit is True


def test_lazy_directory_creation_on_instantiation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that neither Settings nor LocalDatasetStorage eagerly creates directories on disk."""
    fake_storage = tmp_path / "LazyStorageTarget" / "subdir"
    monkeypatch.setenv("STORAGE_DIR", str(fake_storage))

    settings = config.Settings()
    assert not fake_storage.exists()
    assert not settings.storage_dir.exists()

    storage = LocalDatasetStorage(settings.storage_dir)
    assert storage.storage_dir == fake_storage.resolve()
    assert not fake_storage.exists()
    assert not storage.datasets_dir.exists()

    # Also test default instantiation without arguments
    default_storage = LocalDatasetStorage()
    assert default_storage.storage_dir == config.resolve_default_storage_dir()
    assert not (fake_storage / "datasets").exists()


def test_missing_directory_tolerance_for_default_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that Settings initializes cleanly when default storage directory does not exist."""
    fake_home = tmp_path / "NonExistentHome"
    monkeypatch.setattr(config.Path, "home", lambda: fake_home)
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    settings = config.Settings()
    assert settings.storage_dir == (fake_home / ".data-intelligence-lab" / "storage").resolve()
    assert settings.legacy_storage_detected is False
    assert settings.legacy_storage_status == "ACTIVE_IS_LEGACY"
    assert settings.legacy_datasets_count == 0
    assert settings.legacy_incomplete_count == 0
    assert settings.legacy_error is None
    assert settings.legacy_warning is None


def test_active_is_legacy_with_datasets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that when active storage is legacy storage and contains datasets, ACTIVE_IS_LEGACY is reported."""
    fake_storage = tmp_path / "HomeStorage" / "storage"
    datasets_dir = fake_storage / "datasets" / "ds_test1"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    (datasets_dir / "data.parquet").write_bytes(b"PAR1_MOCK")
    (datasets_dir / "metadata.json").write_text('{"id": "ds_test1"}', encoding="utf-8")

    monkeypatch.setattr(config, "get_legacy_storage_dir", lambda: fake_storage.resolve())
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    settings = config.Settings()
    assert settings.storage_dir == fake_storage.resolve()
    assert settings.legacy_storage_detected is True
    assert settings.legacy_storage_status == "ACTIVE_IS_LEGACY"
    assert settings.legacy_datasets_count == 1
    assert settings.active_datasets_count == 1
    assert settings.legacy_incomplete_count == 0
    assert settings.legacy_warning is None


def test_explicit_override_with_legacy_datasets_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that when explicit storage is set and legacy contains datasets, EXPLICIT_OVERRIDE is reported."""
    fake_legacy = tmp_path / "LegacyStorage"
    ds_legacy = fake_legacy / "datasets" / "ds_legacy1"
    ds_legacy.mkdir(parents=True, exist_ok=True)
    (ds_legacy / "data.parquet").write_bytes(b"PAR1")
    (ds_legacy / "metadata.json").write_text("{}", encoding="utf-8")

    fake_explicit = tmp_path / "ExplicitStorage"
    monkeypatch.setattr(config, "get_legacy_storage_dir", lambda: fake_legacy.resolve())
    monkeypatch.setenv("STORAGE_DIR", str(fake_explicit))

    settings = config.Settings()
    assert settings.storage_dir == fake_explicit.resolve()
    assert settings.storage_is_explicit is True
    assert settings.legacy_storage_detected is True
    assert settings.legacy_storage_status == "EXPLICIT_OVERRIDE"
    assert settings.legacy_datasets_count == 1
    assert settings.legacy_warning is not None
    assert "Explicit storage directory" in settings.legacy_warning
    assert "were not moved or altered" in settings.legacy_warning


def test_explicit_override_with_dual_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that when both legacy and explicit storage contain valid datasets, EXPLICIT_OVERRIDE reflects both counts."""
    fake_legacy = tmp_path / "LegacyStorage"
    ds_leg = fake_legacy / "datasets" / "ds_leg"
    ds_leg.mkdir(parents=True, exist_ok=True)
    (ds_leg / "data.parquet").write_bytes(b"PAR1")
    (ds_leg / "metadata.json").write_text("{}", encoding="utf-8")

    fake_explicit = tmp_path / "ExplicitStorage"
    ds_exp = fake_explicit / "datasets" / "ds_exp"
    ds_exp.mkdir(parents=True, exist_ok=True)
    (ds_exp / "data.parquet").write_bytes(b"PAR1")
    (ds_exp / "metadata.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config, "get_legacy_storage_dir", lambda: fake_legacy.resolve())
    monkeypatch.setenv("STORAGE_DIR", str(fake_explicit))

    settings = config.Settings()
    assert settings.storage_dir == fake_explicit.resolve()
    assert settings.legacy_datasets_count == 1
    assert settings.active_datasets_count == 1
    assert settings.legacy_storage_status == "EXPLICIT_OVERRIDE"


def test_incomplete_legacy_artifacts_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that legacy dataset missing metadata.json or data.parquet is reported as incomplete."""
    fake_legacy = tmp_path / "LegacyStorage"
    ds_incomp = fake_legacy / "datasets" / "ds_incomp"
    ds_incomp.mkdir(parents=True, exist_ok=True)
    (ds_incomp / "data.parquet").write_bytes(b"PAR1")

    monkeypatch.setattr(config, "get_legacy_storage_dir", lambda: fake_legacy.resolve())
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    settings = config.Settings()
    assert settings.legacy_storage_detected is True
    assert settings.legacy_storage_status == "ACTIVE_IS_LEGACY"
    assert settings.legacy_datasets_count == 0
    assert settings.legacy_incomplete_count == 1


def test_permission_error_during_legacy_inspection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that PermissionError during legacy inspection fails closed and reports error."""
    fake_legacy = tmp_path / "LockedLegacy"
    (fake_legacy / "datasets").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "get_legacy_storage_dir", lambda: fake_legacy.resolve())
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    def mock_scan(p):
        return 0, 0, "PermissionError"

    monkeypatch.setattr(config, "scan_dataset_directories", mock_scan)

    settings = config.Settings()
    assert settings.legacy_storage_detected is True
    assert settings.legacy_error == "PermissionError"


def test_local_dataset_storage_adapter_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that LocalDatasetStorage uses default storage_dir by default."""
    monkeypatch.delenv("STORAGE_DIR", raising=False)

    storage = LocalDatasetStorage()
    expected = config.resolve_default_storage_dir()

    assert storage.storage_dir == expected
    assert storage.datasets_dir == expected / "datasets"


def test_real_user_dataset_integrity() -> None:
    """Verifies that pre-existing real user datasets are accessible, intact, and non-empty."""
    real_legacy = Path.home() / ".data-intelligence-lab" / "storage" / "datasets" / "ds_37b797c639cc47c3"
    if real_legacy.exists():
        data_file = real_legacy / "data.parquet"
        meta_file = real_legacy / "metadata.json"
        assert data_file.is_file()
        assert meta_file.is_file()
        assert data_file.stat().st_size == 10274
        assert meta_file.stat().st_size == 19250
