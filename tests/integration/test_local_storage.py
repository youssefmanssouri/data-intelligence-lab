"""
Integration Test Suite for Milestone 4A: Local Dataset Storage Adapter
Validates Parquet and metadata serialization, temporal preservation, error boundaries,
path traversal defense, Windows portability, cleanup behavior, and absence of raw CSV values.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import io
import json
import math
from pathlib import Path
import shutil
from typing import Optional
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.adapters.exceptions import (
    DatasetAlreadyExistsError,
    DatasetCorruptError,
    DatasetNotFoundError,
    InvalidDatasetIdError,
    ParquetConversionError,
    StorageError,
)
from backend.adapters.local_storage import (
    DatasetPaths,
    LocalDatasetStorage,
    LocalStorageAdapter,
    format_timezone_offset,
    sanitize_filename,
    serialize_value,
    validate_dataset_id,
)
from core import ingest_csv


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def storage(tmp_path: Path) -> LocalDatasetStorage:
    """Uses standard isolated pytest tmp_path without any repository-local fallback."""
    storage_dir = tmp_path / "dil_storage"
    return LocalDatasetStorage(storage_dir=storage_dir)


# =====================================================================
# 1. Temporal Preservation & Mixed-Offset Enforcement (Blocker 1)
# =====================================================================

def test_temporal_single_aware_utc_round_trip(storage: LocalDatasetStorage) -> None:
    """Single aware datetime with UTC offset round-trips with exact offset and instant."""
    csv_content = b"event_time\n2024-01-01T10:00:00Z\n"
    res = ingest_csv(csv_content)
    dataset_id = "ds_temp_utc"

    storage.save_dataset(dataset_id, res)
    table = storage.read_parquet_table(dataset_id)
    reloaded_dt = table["event_time"][0].as_py()

    expected_dt = datetime.datetime(2024, 1, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)
    assert reloaded_dt == expected_dt
    assert reloaded_dt.utcoffset() == datetime.timedelta(0)
    assert reloaded_dt.hour == 10


def test_temporal_single_aware_non_utc_offset_round_trip(storage: LocalDatasetStorage) -> None:
    """Single aware datetime with non-UTC offset (+02:00) preserves exact offset and wall-clock."""
    csv_content = b"event_time\n2024-01-01T12:00:00+02:00\n"
    res = ingest_csv(csv_content)
    dataset_id = "ds_temp_non_utc"

    storage.save_dataset(dataset_id, res)
    table = storage.read_parquet_table(dataset_id)
    reloaded_dt = table["event_time"][0].as_py()

    tz_expected = datetime.timezone(datetime.timedelta(hours=2))
    assert reloaded_dt.utcoffset() == datetime.timedelta(hours=2)
    assert reloaded_dt.hour == 12
    assert reloaded_dt.minute == 0


def test_temporal_multiple_rows_same_positive_offset(storage: LocalDatasetStorage) -> None:
    """Multiple rows sharing the same positive offset (+05:30) preserve exact wall-clock and offset."""
    csv_content = (
        b"event_time\n"
        b"2024-01-01T09:30:00+05:30\n"
        b"2024-01-02T18:45:00+05:30\n"
    )
    res = ingest_csv(csv_content)
    dataset_id = "ds_temp_pos_offset"

    storage.save_dataset(dataset_id, res)
    table = storage.read_parquet_table(dataset_id)
    reloaded_vals = table["event_time"].to_pylist()

    assert len(reloaded_vals) == 2
    for dt, exp_h, exp_m in zip(reloaded_vals, [9, 18], [30, 45]):
        assert dt.utcoffset() == datetime.timedelta(hours=5, minutes=30)
        assert dt.hour == exp_h
        assert dt.minute == exp_m


def test_temporal_negative_offset_and_microseconds(storage: LocalDatasetStorage) -> None:
    """Negative offset (-02:30) with microseconds (.123456) and None is losslessly preserved."""
    csv_content = (
        b"id,event_time\n"
        b"1,2024-06-15T14:20:30.123456-02:30\n"
        b"2,\n"
        b"3,2024-06-16T08:00:00.000000-02:30\n"
    )
    res = ingest_csv(csv_content)
    dataset_id = "ds_temp_neg_micro"

    storage.save_dataset(dataset_id, res)
    table = storage.read_parquet_table(dataset_id)
    reloaded_vals = table["event_time"].to_pylist()

    assert len(reloaded_vals) == 3
    # Row 0
    assert reloaded_vals[0].utcoffset() == datetime.timedelta(hours=-2, minutes=-30)
    assert reloaded_vals[0].microsecond == 123456
    assert reloaded_vals[0].hour == 14
    # Row 1 (None)
    assert reloaded_vals[1] is None
    # Row 2
    assert reloaded_vals[2].utcoffset() == datetime.timedelta(hours=-2, minutes=-30)
    assert reloaded_vals[2].microsecond == 0
    assert reloaded_vals[2].hour == 8


def test_temporal_mixed_offsets_explicitly_rejected_before_publication(
    storage: LocalDatasetStorage,
) -> None:
    """
    Mixed offsets (+01:00 and -02:30) in the same column MUST NOT be silently normalized.
    The adapter must explicitly reject the column with ParquetConversionError before publication.
    """
    csv_content = (
        b"event_time\n"
        b"2024-01-01T12:00:00+01:00\n"
        b"2024-01-01T12:00:00-02:30\n"
    )
    res = ingest_csv(csv_content)
    dataset_id = "ds_temp_mixed_reject"

    with pytest.raises(ParquetConversionError) as exc_info:
        storage.save_dataset(dataset_id, res)

    # Error message must identify column and mixed offsets
    err_msg = exc_info.value.message
    assert "event_time" in err_msg
    assert "mixed timezone offsets" in err_msg
    assert "+01:00" in err_msg
    assert "-02:30" in err_msg

    # Publication must not occur; staging directory must be cleaned up
    assert storage.dataset_exists(dataset_id) is False
    assert (storage.datasets_dir / dataset_id).exists() is False
    assert len(list(storage.datasets_dir.glob(".tmp_*"))) == 0


def test_temporal_naive_datetime_preserves_naive_representation(
    storage: LocalDatasetStorage,
) -> None:
    """Naive datetimes are preserved as naive timestamp[us] without any injected timezone."""
    csv_content = b"event_time\n2024-01-01T12:00:00\n2024-01-02T15:30:00.654321\n"
    res = ingest_csv(csv_content)
    dataset_id = "ds_temp_naive"

    storage.save_dataset(dataset_id, res)
    table = storage.read_parquet_table(dataset_id)
    schema = table.schema

    assert schema.field("event_time").type == pa.timestamp("us")
    reloaded_vals = table["event_time"].to_pylist()
    assert reloaded_vals[0].tzinfo is None
    assert reloaded_vals[0].hour == 12
    assert reloaded_vals[1].tzinfo is None
    assert reloaded_vals[1].microsecond == 654321


# =====================================================================
# 2. Storage Error Boundary, Path Redaction & Negative Tests (F02)
# =====================================================================

def test_parquet_write_failure_maps_to_storage_error_and_sanitizes(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated write failure produces StorageError without leaking absolute host paths or context."""
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_err_pq_write"

    def mock_write_table(*args: object, **kwargs: object) -> None:
        raise OSError("C:\\Users\\User\\secret\\disk_fail.bin: write failed")

    monkeypatch.setattr(pq, "write_table", mock_write_table)

    with pytest.raises(StorageError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, StorageError)
    assert err.code == "ERR_STORAGE_FAILED"
    assert "Failed to write Parquet data" in err.message
    # No user home or absolute filesystem path leaked
    assert "C:\\Users" not in str(err)
    assert "secret" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    # Staging must be cleaned up
    assert storage.dataset_exists(dataset_id) is False
    assert len(list(storage.datasets_dir.glob(".tmp_*"))) == 0


def test_metadata_write_failure_maps_to_storage_error_and_sanitizes(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated metadata write failure produces StorageError without leaking paths or context."""
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_err_meta_write"

    def mock_json_dump(*args: object, **kwargs: object) -> None:
        raise OSError("Permission denied: C:\\Users\\User\\AppData\\Local\\disk.json")

    monkeypatch.setattr(json, "dump", mock_json_dump)

    with pytest.raises(StorageError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, StorageError)
    assert err.code == "ERR_STORAGE_FAILED"
    assert "Failed to write metadata" in err.message
    assert "C:\\Users" not in str(err)
    assert "AppData" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    assert storage.dataset_exists(dataset_id) is False


def test_cleanup_staging_inspection_failure_preserves_primary_exception(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Failure A: When primary Parquet write fails and staging_dir.exists() raises PermissionError,
    the primary StorageError remains authoritative and no host path or unsuppressed context leaks.
    """
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_err_cleanup_inspection"

    def mock_write_table(*args: object, **kwargs: object) -> None:
        raise OSError("C:\\secret\\host\\path\\write_error.bin: write failed")

    real_exists = Path.exists

    def mock_exists(self: Path) -> bool:
        if ".tmp_" in str(self):
            raise PermissionError(13, "Permission denied to staging dir", "C:\\secret\\host\\path\\staging")
        return real_exists(self)

    monkeypatch.setattr(pq, "write_table", mock_write_table)
    monkeypatch.setattr(Path, "exists", mock_exists)

    with pytest.raises(StorageError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, StorageError)
    assert not isinstance(err, PermissionError)
    assert "Failed to write Parquet data" in err.message
    assert "secret" not in str(err)
    assert "host" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_cleanup_deletion_failure_preserves_primary_exception(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When primary write fails and shutil.rmtree raises PermissionError, the primary StorageError
    is preserved and cleanup remains safely best-effort.
    """
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_err_cleanup_deletion"

    def mock_write_table(*args: object, **kwargs: object) -> None:
        raise OSError("C:\\secret\\host\\path\\write_error.bin: write failed")

    def mock_rmtree(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied during rmtree", "C:\\secret\\host\\path\\rmtree")

    monkeypatch.setattr(pq, "write_table", mock_write_table)
    monkeypatch.setattr(shutil, "rmtree", mock_rmtree)

    with pytest.raises(StorageError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, StorageError)
    assert not isinstance(err, PermissionError)
    assert "Failed to write Parquet data" in err.message
    assert "secret" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_publication_replace_failure_maps_to_storage_error(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure during atomic directory publication maps to sanitized StorageError."""
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_err_pub_replace"

    def mock_replace(self: Path, target: Path) -> Path:
        raise OSError(5, "Access is denied: C:\\secret\\target\\dir")

    monkeypatch.setattr(Path, "replace", mock_replace)

    with pytest.raises(StorageError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, StorageError)
    assert "Failed to publish dataset" in err.message
    assert "secret" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_publication_secondary_inspection_failure_does_not_leak_or_replace(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Failure B: When publication replace raises PermissionError and the secondary target_dir.exists()
    inspection also raises PermissionError, the error is safely contained and sanitized.
    """
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_err_pub_secondary_inspection"

    def mock_replace(self: Path, target: Path) -> Path:
        raise PermissionError(13, "Permission denied on rename", "C:\\secret\\staging_replace")

    real_exists = Path.exists
    exists_call_count = 0

    def mock_exists(self: Path) -> bool:
        nonlocal exists_call_count
        if str(self).endswith(dataset_id):
            exists_call_count += 1
            if exists_call_count == 1:
                return False  # initial pre-check: dataset does not exist yet
            raise PermissionError(13, "Permission denied on target inspection", "C:\\secret\\target_exists")
        return real_exists(self)

    monkeypatch.setattr(Path, "replace", mock_replace)
    monkeypatch.setattr(Path, "exists", mock_exists)

    with pytest.raises(StorageError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, StorageError)
    assert not isinstance(err, PermissionError)
    assert "Failed to publish dataset" in err.message
    assert "secret" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_publication_target_already_exists_detection(
    storage: LocalDatasetStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When publication replace fails and target_dir.exists() is True, DatasetAlreadyExistsError is raised."""
    res = ingest_csv(b"col\n1\n")
    dataset_id = "ds_pub_exists_collision"

    def mock_replace(self: Path, target: Path) -> Path:
        raise FileExistsError("Target directory exists")

    real_exists = Path.exists
    exists_call_count = 0

    def mock_exists(self: Path) -> bool:
        nonlocal exists_call_count
        if str(self).endswith(dataset_id):
            exists_call_count += 1
            if exists_call_count == 1:
                return False  # initial pre-check: dataset does not exist yet
            return True       # publication secondary check: target was created concurrently
        return real_exists(self)

    monkeypatch.setattr(Path, "replace", mock_replace)
    monkeypatch.setattr(Path, "exists", mock_exists)

    with pytest.raises(DatasetAlreadyExistsError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, DatasetAlreadyExistsError)
    assert err.dataset_id == dataset_id
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_load_metadata_invalid_utf8_raises_sanitized_corrupt_error(
    storage: LocalDatasetStorage,
) -> None:
    """
    Failure C: Loading metadata with invalid UTF-8 bytes (e.g. b'\\xff') raises sanitized DatasetCorruptError.
    """
    dataset_id = "ds_corrupt_utf8"
    target_dir = storage.datasets_dir / dataset_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "data.parquet").write_bytes(b"PAR1_stub_data")
    (target_dir / "metadata.json").write_bytes(b"\xff\xfe\x00\x12invalid_utf8")

    with pytest.raises(DatasetCorruptError) as exc_info:
        storage.load_metadata(dataset_id)

    err = exc_info.value
    assert isinstance(err, DatasetCorruptError)
    assert issubclass(type(err), StorageError)
    assert err.dataset_id == dataset_id
    assert "invalid UTF-8 encoding" in err.message
    assert str(storage.storage_dir) not in err.message
    assert "Users" not in err.message
    assert "codec can't decode" not in err.message
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_load_metadata_malformed_json_raises_sanitized_corrupt_error(
    storage: LocalDatasetStorage,
) -> None:
    """Loading metadata with malformed JSON syntax raises sanitized DatasetCorruptError with line info."""
    dataset_id = "ds_corrupt_bad_json"
    target_dir = storage.datasets_dir / dataset_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "data.parquet").write_bytes(b"PAR1_stub_data")
    (target_dir / "metadata.json").write_text("{broken json syntax:", encoding="utf-8")

    with pytest.raises(DatasetCorruptError) as exc_info:
        storage.load_metadata(dataset_id)

    err = exc_info.value
    assert isinstance(err, DatasetCorruptError)
    assert issubclass(type(err), StorageError)
    assert err.dataset_id == dataset_id
    assert "Invalid JSON in metadata" in err.message
    assert str(storage.storage_dir) not in err.message
    assert "Users" not in err.message
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_conversion_failure_preserves_exception_and_cleans_up_staging(
    storage: LocalDatasetStorage,
) -> None:
    """Conversion failures (e.g. mixed timezone offsets) clean up staging directory and preserve ParquetConversionError."""
    csv_content = b"event_time\n2024-01-01T10:00:00+01:00\n2024-01-01T10:00:00-02:30\n"
    res = ingest_csv(csv_content)
    dataset_id = "ds_conversion_cleanup"

    with pytest.raises(ParquetConversionError) as exc_info:
        storage.save_dataset(dataset_id, res)

    err = exc_info.value
    assert isinstance(err, ParquetConversionError)
    assert issubclass(type(err), StorageError)
    assert "mixed timezone offsets" in err.message
    # Verify staging directory was deleted
    assert storage.dataset_exists(dataset_id) is False
    assert (storage.datasets_dir / dataset_id).exists() is False
    assert len(list(storage.datasets_dir.glob(".tmp_*"))) == 0


def test_corrupt_parquet_produces_sanitized_corruption_exception(
    storage: LocalDatasetStorage,
) -> None:
    """Reading corrupt Parquet file raises DatasetCorruptError without leaking PyArrow paths."""
    dataset_id = "ds_corrupt_pq_sanitized"
    target_dir = storage.datasets_dir / dataset_id
    target_dir.mkdir(parents=True, exist_ok=True)

    # Write corrupt non-Parquet binary and valid metadata
    (target_dir / "data.parquet").write_bytes(b"NOT_A_PARQUET_FILE_CORRUPTED_BYTES")
    (target_dir / "metadata.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DatasetCorruptError) as exc_info:
        storage.read_parquet_table(dataset_id)

    err = exc_info.value
    assert err.code == "ERR_DATASET_CORRUPT"
    assert err.dataset_id == dataset_id
    assert "Parquet data artifact is corrupted or unreadable." in err.message
    # Verify absolute filesystem path is not exposed
    assert str(storage.storage_dir) not in err.message
    assert "Users" not in err.message
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_dataset_paths_repr_does_not_leak_absolute_path(storage: LocalDatasetStorage) -> None:
    """DatasetPaths repr only includes dataset_id and never exposes absolute local filesystem paths."""
    res = ingest_csv(b"a\n1\n")
    dataset_id = "ds_paths_repr"
    storage.save_dataset(dataset_id, res)

    paths = storage.get_dataset_paths(dataset_id)
    repr_str = repr(paths)
    assert dataset_id in repr_str
    assert str(storage.storage_dir) not in repr_str
    assert "Users" not in repr_str


# =====================================================================
# 3. Metadata Privacy Contract (Finding 3)
# =====================================================================

def test_metadata_does_not_contain_raw_csv_cell_values(storage: LocalDatasetStorage) -> None:
    """
    Verifies that raw CSV text, row-level records, and private cell values
    are completely excluded from metadata.json under the supported persistence contract.
    """
    secret_token = "TOP_SECRET_CREDENTIAL_XYZ_987654"
    secret_note = "Restricted Internal Note Alpha"
    csv_content = f"id,user,secret_col\n1,alice,{secret_token}\n2,bob,{secret_note}\n".encode()

    res = ingest_csv(csv_content)
    dataset_id = "ds_privacy_strict"

    storage.save_dataset(dataset_id, res, filename="confidential.csv")

    paths = storage.get_dataset_paths(dataset_id)
    raw_meta_text = paths.metadata_path.read_text(encoding="utf-8")
    meta_json = json.loads(raw_meta_text)

    # 1. Secret cell contents must NOT be in metadata.json
    assert secret_token not in raw_meta_text
    assert secret_note not in raw_meta_text

    # 2. No records or sample values
    assert "records" not in meta_json
    assert "csv_content" not in meta_json
    assert "raw_csv" not in meta_json
    for col in meta_json["columns"]:
        assert "sample_values" not in col

    # 3. Verify the secret data IS preserved in data.parquet
    table = storage.read_parquet_table(dataset_id)
    pylist = table.to_pylist()
    assert pylist[0]["secret_col"] == secret_token
    assert pylist[1]["secret_col"] == secret_note


# =====================================================================
# 4. Generic Serialization Hardening (Finding 5)
# =====================================================================

def test_serialize_value_handles_primitives_and_collections() -> None:
    """serialize_value handles primitives, dates, and nested collections recursively."""
    assert serialize_value(None) is None
    assert serialize_value(True) is True
    assert serialize_value(42) == 42
    assert serialize_value(3.14) == 3.14
    assert serialize_value("text") == "text"
    assert serialize_value(datetime.date(2024, 1, 1)) == "2024-01-01"
    assert serialize_value(datetime.datetime(2024, 1, 1, 12, 0, 0)) == "2024-01-01T12:00:00"
    assert serialize_value(Decimal("12.34")) == "12.34"
    assert serialize_value([1, "a", None]) == [1, "a", None]
    assert serialize_value((1, 2)) == [1, 2]
    assert serialize_value({2, 1}) == [1, 2]
    assert serialize_value({"key": [Decimal("5.5")]}) == {"key": ["5.5"]}


def test_serialize_value_rejects_unsupported_objects() -> None:
    """serialize_value raises TypeError on unsupported arbitrary object to prevent silent corruption."""
    class CustomUnsupported:
        pass

    with pytest.raises(TypeError) as exc_info:
        serialize_value(CustomUnsupported())
    assert "Unsupported metadata value type" in str(exc_info.value)


# =====================================================================
# 5. Windows Portability & Path Length Limits (Finding 7)
# =====================================================================

@pytest.mark.parametrize("reserved_name", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "con", "nul"])
def test_windows_reserved_device_names_rejected_as_dataset_ids(
    storage: LocalDatasetStorage, reserved_name: str
) -> None:
    """Windows reserved device names are safely rejected with InvalidDatasetIdError."""
    res = ingest_csv(b"col\n1\n")
    with pytest.raises(InvalidDatasetIdError) as exc_info:
        storage.save_dataset(reserved_name, res)
    assert "reserved system device name" in str(exc_info.value)
    assert storage.dataset_exists(reserved_name) is False


def test_sanitize_filename_guards_windows_reserved_names() -> None:
    """Filename sanitization prefixes Windows reserved names with dataset_."""
    assert sanitize_filename("CON.csv") == "dataset_CON.csv"
    assert sanitize_filename("nul.csv") == "dataset_nul.csv"
    assert sanitize_filename("COM1") == "dataset_COM1"
    assert sanitize_filename("normal.csv") == "normal.csv"


def test_storage_path_length_limit_guard(storage: LocalDatasetStorage) -> None:
    """Paths exceeding maximum supported storage limit (250 chars) are cleanly rejected."""
    res = ingest_csv(b"col\n1\n")
    # 128 characters is the max valid identifier length
    long_id = "a" * 128
    # Create an artificially deep storage root to exceed 250 characters
    deep_storage = LocalDatasetStorage(
        storage_dir=storage.storage_dir / ("deep_dir_" * 15)
    )
    with pytest.raises(StorageError) as exc_info:
        deep_storage.save_dataset(long_id, res)
    assert "exceeds maximum supported length" in exc_info.value.message
    # Verify path is not leaked in the message
    assert "deep_dir_" not in exc_info.value.message


# =====================================================================
# 6. Basic Round-Trip & Alignment Regressions
# =====================================================================

def test_parquet_and_metadata_successful_round_trip(storage: LocalDatasetStorage) -> None:
    """Verifies complete save and reload of Parquet data and JSON metadata."""
    csv_content = (
        b"id,amount,is_active,event_date,event_time,notes\n"
        b"1,19.99,true,2024-01-15,2024-01-15T14:30:00,First order\n"
        b"2,49.50,false,2024-02-20,2024-02-20T09:15:00,Second order\n"
        b"3,0.0,true,2024-03-01,2024-03-01T00:00:00,\n"
    )
    result = ingest_csv(csv_content)
    dataset_id = "ds_01h8x9p2q1m4"

    storage.save_dataset(
        dataset_id=dataset_id,
        ingestion_result=result,
        filename="orders_q1.csv",
    )

    assert storage.dataset_exists(dataset_id) is True

    meta_loaded = storage.load_metadata(dataset_id)
    assert meta_loaded["dataset_id"] == dataset_id
    assert meta_loaded["filename"] == "orders_q1.csv"
    assert meta_loaded["row_count"] == 3
    assert meta_loaded["column_count"] == 6
    assert meta_loaded["encoding"] == "utf-8"
    assert meta_loaded["delimiter"] == ","
    assert meta_loaded["data_quality_score"] == result.profile.quality.overall_score
    assert len(meta_loaded["columns"]) == 6
    assert len(meta_loaded["column_metadata"]) == 6

    table = storage.read_parquet_table(dataset_id)
    assert table.num_rows == 3
    assert table.num_columns == 6
    assert table.column_names == ["id", "amount", "is_active", "event_date", "event_time", "notes"]


def test_preservation_of_row_column_alignment_and_inferred_types(storage: LocalDatasetStorage) -> None:
    """Verifies that all inferred primitive types map correctly to Arrow/Parquet types."""
    csv_content = (
        b"int_col,float_col,neg_zero,bool_col,date_col,dt_col,str_col\n"
        b"42,3.14159,-0.0,true,2024-06-15,2024-06-15T12:00:00,alpha\n"
        b",,,false,,,\n"
    )
    result = ingest_csv(csv_content)
    dataset_id = "ds_types_align_01"

    storage.save_dataset(dataset_id, result, filename="alignment.csv")
    table = storage.read_parquet_table(dataset_id)
    schema = table.schema

    assert schema.field("int_col").type == pa.int64()
    assert schema.field("float_col").type == pa.float64()
    assert schema.field("neg_zero").type == pa.float64()
    assert schema.field("bool_col").type == pa.bool_()
    assert schema.field("date_col").type == pa.date32()
    assert schema.field("dt_col").type == pa.timestamp("us")
    assert schema.field("str_col").type == pa.string()

    pylist = table.to_pylist()
    assert len(pylist) == 2
    row0 = pylist[0]
    assert row0["int_col"] == 42
    assert abs(row0["float_col"] - 3.14159) < 1e-6
    assert math.copysign(1.0, row0["neg_zero"]) == -1.0
    assert row0["bool_col"] is True
    assert row0["date_col"] == datetime.date(2024, 6, 15)
    assert row0["dt_col"] == datetime.datetime(2024, 6, 15, 12, 0, 0)
    assert row0["str_col"] == "alpha"


def test_duplicate_identifier_raises_error_and_preserves_original(
    storage: LocalDatasetStorage,
) -> None:
    """Verifies that re-using an existing dataset ID raises DatasetAlreadyExistsError."""
    res1 = ingest_csv(b"col_a\n100\n")
    res2 = ingest_csv(b"col_b\n999\n")
    dataset_id = "ds_duplicate_guard"

    storage.save_dataset(dataset_id, res1, filename="original.csv")
    assert storage.dataset_exists(dataset_id) is True

    with pytest.raises(DatasetAlreadyExistsError) as exc_info:
        storage.save_dataset(dataset_id, res2, filename="overwrite_attempt.csv")
    assert exc_info.value.dataset_id == dataset_id

    meta = storage.load_metadata(dataset_id)
    assert meta["filename"] == "original.csv"
    table = storage.read_parquet_table(dataset_id)
    assert table.column_names == ["col_a"]
    assert table["col_a"].to_pylist() == [100]


def test_storage_dir_with_spaces_and_unicode(tmp_path: Path) -> None:
    """Verifies that persistence works across directory paths containing spaces and non-ASCII Unicode."""
    unicode_storage_dir = tmp_path / "Espace Données" / "📊 Labo d'Analytique (2026)"
    unicode_storage = LocalDatasetStorage(storage_dir=unicode_storage_dir)

    result = ingest_csv(b"city,temp\nCasablanca,25.5\nMontr\xc3\xa9al,-5.0\n")
    dataset_id = "ds_unicode_path_01"

    unicode_storage.save_dataset(dataset_id, result, filename="données_météo.csv")

    assert unicode_storage.dataset_exists(dataset_id) is True
    meta = unicode_storage.load_metadata(dataset_id)
    assert meta["dataset_id"] == dataset_id
    assert "données_météo.csv" in meta["filename"]

    table = unicode_storage.read_parquet_table(dataset_id)
    assert table.num_rows == 2
    assert table["city"].to_pylist() == ["Casablanca", "Montréal"]


def test_delete_dataset_lifecycle(storage: LocalDatasetStorage) -> None:
    """Verifies complete deletion of a persisted dataset."""
    result = ingest_csv(b"a,b\n1,2\n")
    dataset_id = "ds_delete_me"

    storage.save_dataset(dataset_id, result)
    assert storage.dataset_exists(dataset_id) is True

    deleted = storage.delete_dataset(dataset_id)
    assert deleted is True
    assert storage.dataset_exists(dataset_id) is False

    deleted_again = storage.delete_dataset(dataset_id)
    assert deleted_again is False


def test_header_only_dataset_round_trip(storage: LocalDatasetStorage) -> None:
    """Verifies that a 0-row header-only dataset produces a valid 0-row Parquet table."""
    result = ingest_csv(b"col_a,col_b,col_c\n")
    dataset_id = "ds_zero_rows"

    storage.save_dataset(dataset_id, result, filename="empty.csv")
    assert storage.dataset_exists(dataset_id) is True

    table = storage.read_parquet_table(dataset_id)
    assert table.num_rows == 0
    assert table.num_columns == 3
    assert table.column_names == ["col_a", "col_b", "col_c"]


def test_adapter_alias_compatibility() -> None:
    """Verifies that LocalStorageAdapter is an alias to LocalDatasetStorage."""
    assert LocalStorageAdapter is LocalDatasetStorage
