"""
Storage durability: both dataset files are fsynced before the publishing rename,
and staging directories abandoned by a killed process are swept at startup without
touching another process's in-flight write.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import time

import pytest

from backend.adapters import local_storage
from backend.adapters.local_execution import LocalExecutionAdapter
from backend.adapters.local_storage import STALE_STAGING_SECONDS, LocalDatasetStorage
from backend.app import create_app

CSV = b"id,name\n1,alpha\n2,beta\n"


def test_both_files_are_fsynced_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[str] = []
    real_fsync, real_replace = os.fsync, Path.replace
    published_after: list[int] = []

    def tracking_fsync(fd: int) -> None:
        synced.append(os.path.basename(os.readlink(f"/proc/self/fd/{fd}")) if os.path.exists("/proc/self/fd") else str(fd))
        real_fsync(fd)

    def tracking_replace(self: Path, target: Path):  # type: ignore[no-untyped-def]
        published_after.append(len(synced))
        return real_replace(self, target)

    monkeypatch.setattr(local_storage.os, "fsync", tracking_fsync)
    monkeypatch.setattr(Path, "replace", tracking_replace)
    adapter = LocalExecutionAdapter(storage_dir=tmp_path / "storage")
    meta = adapter.ingest_and_store(file_source=io.BytesIO(CSV), filename="d.csv")
    assert len(synced) == 2, synced
    if os.path.exists("/proc/self/fd"):
        assert sorted(synced) == ["data.parquet", "metadata.json"]
    assert published_after == [2], "the rename must come after both fsyncs"
    table = adapter.storage.read_parquet_table(meta["dataset_id"])
    assert table.num_rows == 2


def _staging(root: Path, name: str, age_seconds: float) -> Path:
    d = root / "datasets" / name
    d.mkdir(parents=True)
    (d / "data.parquet").write_bytes(b"partial")
    stamp = time.time() - age_seconds
    os.utime(d, (stamp, stamp))
    return d


def test_sweep_removes_only_old_abandoned_staging(tmp_path: Path) -> None:
    storage = LocalDatasetStorage(storage_dir=tmp_path)
    old = _staging(tmp_path, ".tmp_" + "a" * 32 + "_ds_old", STALE_STAGING_SECONDS + 60)
    fresh = _staging(tmp_path, ".tmp_" + "b" * 32 + "_ds_fresh", 30)
    dataset = _staging(tmp_path, "ds_0123456789abcdef", STALE_STAGING_SECONDS * 10)
    stray_file = tmp_path / "datasets" / ".tmp_not_a_dir"
    stray_file.write_text("x", encoding="utf-8")
    os.utime(stray_file, (0, 0))

    assert storage.sweep_stale_staging() == 1
    assert not old.exists()
    assert fresh.is_dir(), "another process's in-flight write must be left alone"
    assert dataset.is_dir(), "published datasets are never swept, however old"
    assert stray_file.is_file()


def test_sweep_never_raises_without_a_datasets_dir(tmp_path: Path) -> None:
    assert LocalDatasetStorage(storage_dir=tmp_path / "missing").sweep_stale_staging() == 0


def test_app_startup_sweeps_abandoned_staging(tmp_path: Path) -> None:
    old = _staging(tmp_path, ".tmp_" + "c" * 32 + "_ds_old", STALE_STAGING_SECONDS + 60)
    create_app(storage_dir=tmp_path)
    assert not old.exists()
