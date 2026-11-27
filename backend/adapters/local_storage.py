"""
Data Intelligence Lab - Local Dataset Storage Adapter
Implements atomic local persistence of normalized tabular datasets to Apache Parquet
and structural/statistical metadata to JSON under the architecture-specified layout:
    ~/.data-intelligence-lab/storage/datasets/{dataset_id}/
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from backend.adapters.exceptions import (
    DatasetAlreadyExistsError,
    DatasetCorruptError,
    DatasetNotFoundError,
    InvalidDatasetIdError,
    ParquetConversionError,
    StorageError,
)
from core.types import (
    ColumnMetadata,
    ColumnProfile,
    DataQualityReport,
    DatasetProfile,
    IngestionResult,
)


# Strict identifier regex: 1 to 128 alphanumeric characters, underscores, or hyphens.
# Must start with an alphanumeric character.
DATASET_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

# Windows reserved device names (case-insensitive)
WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

# Path length limit guard for Windows portability (MAX_PATH is 260; 250 allows headroom for artifacts)
MAX_STORAGE_PATH_LENGTH = 250
# Staging directories older than this are abandoned writes (see sweep_stale_staging).
STALE_STAGING_SECONDS = 24 * 60 * 60


def validate_dataset_id(dataset_id: str) -> str:
    """
    Validates a dataset identifier, enforcing strict character constraints and
    preventing path traversal attacks (e.g., '..', '/', '\\', null bytes) and
    Windows reserved device names.

    Returns:
        The validated dataset identifier string.

    Raises:
        InvalidDatasetIdError: If the identifier is malformed, attempts traversal,
                               exceeds length bounds, or matches reserved device names.
    """
    if not isinstance(dataset_id, str):
        raise InvalidDatasetIdError(
            f"Dataset identifier must be a string, got {type(dataset_id).__name__}."
        )

    cleaned = dataset_id.strip()
    if not cleaned:
        raise InvalidDatasetIdError("Dataset identifier cannot be empty or whitespace.")

    # Explicit check for directory traversal, null bytes, and path separators
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned or "\0" in cleaned:
        raise InvalidDatasetIdError(
            f"Dataset identifier contains invalid path traversal or separator characters: {dataset_id!r}"
        )

    # Windows reserved device names guard
    name_stem = cleaned.split(".")[0].upper()
    if name_stem in WINDOWS_RESERVED_NAMES:
        raise InvalidDatasetIdError(
            f"Dataset identifier {dataset_id!r} is a reserved system device name."
        )

    if len(cleaned) > 128:
        raise InvalidDatasetIdError(
            "Dataset identifier exceeds maximum allowed length of 128 characters."
        )

    if not DATASET_ID_REGEX.match(cleaned):
        raise InvalidDatasetIdError(
            f"Dataset identifier {dataset_id!r} is invalid. "
            "Must be 1-128 characters starting with an alphanumeric character and "
            "containing only alphanumeric characters, underscores, or hyphens."
        )

    return cleaned


def sanitize_filename(filename: Optional[str]) -> str:
    """
    Sanitizes an untrusted source filename to a clean, safe basename.
    Strips directory paths, control characters, dangerous filesystem tokens,
    and guards against Windows reserved device names.
    """
    if not filename or not isinstance(filename, str):
        return "dataset.csv"

    # Normalize Windows and POSIX separators to forward slash, take basename only
    base = Path(filename.replace("\\", "/")).name.strip()

    # Filter non-printable and null characters
    base = "".join(c for c in base if c.isprintable() and c != "\0")

    # Replace forbidden filename characters (<, >, :, ", /, \, |, ?, *) with underscore
    base = re.sub(r'[<>:"/\\|?*]', "_", base).strip(". ")

    if not base:
        return "dataset.csv"

    # Guard against Windows reserved names
    stem = Path(base).stem.upper()
    if stem in WINDOWS_RESERVED_NAMES:
        base = f"dataset_{base}"

    return base


def format_timezone_offset(delta: datetime.timedelta) -> str:
    """Formats a datetime.timedelta offset into ISO-8601 offset string (e.g., '+02:00', '-02:30', 'UTC')."""
    total_seconds = int(delta.total_seconds())
    if total_seconds == 0:
        return "UTC"
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds // 60, 60)
    return f"{sign}{hours:02d}:{remainder:02d}"


def serialize_value(v: Any) -> Any:
    """
    Serializes domain metadata values to JSON-compatible primitives.
    Handles primitives, dates, datetimes, Decimals, and recursive collections.
    Rejects unsupported arbitrary objects with TypeError to prevent silent corruption.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, list):
        return [serialize_value(item) for item in v]
    if isinstance(v, tuple):
        return [serialize_value(item) for item in v]
    if isinstance(v, (set, frozenset)):
        return sorted([serialize_value(item) for item in v])
    if isinstance(v, dict):
        return {str(k): serialize_value(val) for k, val in v.items()}
    raise TypeError(
        f"Unsupported metadata value type for serialization: {type(v).__name__}"
    )


def convert_to_arrow_table(ingestion_result: IngestionResult) -> pa.Table:
    """
    Converts IngestionResult records into a strongly-typed Apache Arrow Table
    aligned with the domain-inferred column types.

    Preserves:
    - boolean -> pa.bool_()
    - integer -> pa.int64()
    - float -> pa.float64() (including -0.0)
    - date -> pa.date32()
    - datetime -> pa.timestamp("us") [naive] or pa.timestamp("us", tz=...) [uniform aware]
    - string -> pa.string()

    Temporal Integrity:
    - Rejects mixed-offset aware datetime columns explicitly before publication.
      Parquet columnar timestamps require a single uniform timezone per column;
      mixed offsets cannot be serialized without loss of original per-value representations.

    Raises:
        ParquetConversionError: If records cannot be converted to Arrow schema
                                or contain unsupported mixed temporal representations.
    """
    if not isinstance(ingestion_result, IngestionResult):
        raise TypeError(
            f"Expected IngestionResult, got {type(ingestion_result).__name__}."
        )

    column_meta = sorted(ingestion_result.column_metadata, key=lambda m: m.index)
    num_rows = ingestion_result.profile.row_count

    if len(ingestion_result.records) != num_rows:
        raise ParquetConversionError(
            f"Record count ({len(ingestion_result.records)}) does not match profile row count ({num_rows})."
        )

    arrays: List[pa.Array] = []
    fields: List[pa.Field] = []

    for m in column_meta:
        norm_name = m.normalized_name
        inferred_type = m.inferred_type
        col_values = [r.get(norm_name) for r in ingestion_result.records]

        try:
            if inferred_type == "boolean":
                arr = pa.array(col_values, type=pa.bool_())
            elif inferred_type == "integer":
                arr = pa.array(col_values, type=pa.int64())
            elif inferred_type == "float":
                arr = pa.array(col_values, type=pa.float64())
            elif inferred_type == "date":
                arr = pa.array(col_values, type=pa.date32())
            elif inferred_type == "datetime":
                non_null_dts = [v for v in col_values if v is not None]
                if not non_null_dts:
                    arr = pa.array(col_values, type=pa.timestamp("us"))
                else:
                    has_aware = any(v.tzinfo is not None for v in non_null_dts)
                    has_naive = any(v.tzinfo is None for v in non_null_dts)

                    if has_aware and has_naive:
                        raise ParquetConversionError(
                            f"Column '{norm_name}' contains a mix of naive and timezone-aware datetimes. "
                            "Parquet columnar storage requires uniform timezone awareness.",
                            column_name=norm_name,
                        )

                    if has_aware:
                        # Collect distinct UTC offsets
                        distinct_offsets = {v.utcoffset() for v in non_null_dts}
                        if len(distinct_offsets) > 1:
                            offset_reprs = sorted([
                                format_timezone_offset(off) if off is not None else "None"
                                for off in distinct_offsets
                            ])
                            raise ParquetConversionError(
                                f"Column '{norm_name}' contains aware datetimes with mixed timezone offsets ({', '.join(offset_reprs)}). "
                                "Parquet columnar storage requires a uniform timezone offset per column; "
                                "mixed offsets cannot be serialized without altering original offset representations.",
                                column_name=norm_name,
                            )
                        # Uniform timezone offset: losslessly preserve exact offset and wall-clock time
                        single_offset = next(iter(distinct_offsets))
                        tz_str = format_timezone_offset(single_offset) if single_offset is not None else "UTC"
                        arr = pa.array(col_values, type=pa.timestamp("us", tz=tz_str))
                    else:
                        # Uniform naive datetimes
                        arr = pa.array(col_values, type=pa.timestamp("us"))
            elif inferred_type == "string":
                arr = pa.array(col_values, type=pa.string())
            else:
                arr = pa.array([str(v) if v is not None else None for v in col_values], type=pa.string())
        except ParquetConversionError:
            raise
        except (OverflowError, pa.ArrowInvalid, TypeError, ValueError) as err:
            raise ParquetConversionError(
                f"Failed to convert column '{norm_name}' (index {m.index}) with inferred type '{inferred_type}' to Parquet/Arrow: {err}",
                column_name=norm_name,
            ) from None

        arrays.append(arr)
        fields.append(pa.field(norm_name, arr.type))

    schema = pa.schema(fields)
    return pa.Table.from_arrays(arrays, schema=schema)


def build_dataset_metadata(
    dataset_id: str,
    ingestion_result: IngestionResult,
    filename: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Serializes domain DatasetProfile and ColumnMetadata structures into stable JSON format.
    Does NOT store raw CSV file contents, sample cell values, or row-level records.
    """
    p = ingestion_result.profile
    q = p.quality
    meta_map = {m.normalized_name: m for m in ingestion_result.column_metadata}

    columns_serialized: List[Dict[str, Any]] = []
    for col in p.columns:
        m = meta_map.get(col.name)
        col_dict: Dict[str, Any] = {
            "index": col.index,
            "name": col.name,
            "raw_name": col.raw_name,
            "inferred_type": col.inferred_type,
            "total_count": col.total_count,
            "null_count": col.null_count,
            "null_ratio": col.null_ratio,
            "distinct_count": col.distinct_count,
            "is_unique": col.is_unique,
            "stats": {k: serialize_value(v) for k, v in col.stats.items()},
            "inference_reason": m.inference_reason if m else "DEFAULT",
            "inference_policy_version": m.inference_policy_version if m else "1.0",
        }
        columns_serialized.append(col_dict)

    column_meta_serialized = [
        {
            "index": m.index,
            "raw_name": m.raw_name,
            "normalized_name": m.normalized_name,
            "inferred_type": m.inferred_type,
            "inference_policy_version": m.inference_policy_version,
            "inference_reason": m.inference_reason,
        }
        for m in sorted(ingestion_result.column_metadata, key=lambda x: x.index)
    ]

    return {
        "dataset_id": dataset_id,
        "filename": sanitize_filename(filename),
        "created_at": created_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "row_count": p.row_count,
        "column_count": p.column_count,
        "encoding": p.encoding,
        "delimiter": p.delimiter,
        "data_quality_score": q.overall_score,
        "quality": {
            "overall_score": q.overall_score,
            "completeness_score": q.completeness_score,
            "uniqueness_score": q.uniqueness_score,
            "validity_score": q.validity_score,
            "total_cells": q.total_cells,
            "total_missing_cells": q.total_missing_cells,
            "duplicate_row_count": q.duplicate_row_count,
            "warnings": list(q.warnings),
        },
        "columns": columns_serialized,
        "column_metadata": column_meta_serialized,
        "warnings": list(p.warnings),
    }


class DatasetPaths:
    """Holds validated filesystem paths for a persisted dataset."""

    def __init__(
        self,
        dataset_id: str,
        dataset_dir: Path,
        data_path: Path,
        metadata_path: Path,
    ) -> None:
        self.dataset_id = dataset_id
        self.dataset_dir = dataset_dir
        self.data_path = data_path
        self.metadata_path = metadata_path

    def __repr__(self) -> str:
        # Sanitized representation: never expose host filesystem paths
        return f"DatasetPaths(dataset_id={self.dataset_id!r})"


class LocalDatasetStorage:
    """
    Local filesystem persistence adapter for Data Intelligence Lab datasets.
    Stores normalized datasets as Apache Parquet ('data.parquet')
    and structural/statistical metadata as JSON ('metadata.json') under
    the architecture-specified hierarchy:
        {storage_dir}/datasets/{dataset_id}/

    Milestone 4A Current Guarantees:
    - No-overwrite behavior: Re-publishing an existing dataset ID is rejected.
    - Artifact presence validation: Existence requires both 'data.parquet' and 'metadata.json'.
    - Isolated staging: Staging writes occur in unique directories (.tmp_{uuid}_{dataset_id}).
    - Atomic publication: Staging directories are renamed into place once fully written.
    - Best-effort cleanup: Staging failures clean up without masking primary errors.
    - Durable publication: both files are fsynced before the publishing rename.
    - Abandoned staging (process killed mid-write) is swept at startup after 24 hours.

    Deferred Controls (to subsequent hardening milestone):
    - OS-level read-only bit enforcement on published files.
    - SHA-256 cryptographic checksum generation and verification.
    """

    def __init__(self, storage_dir: Optional[Union[str, Path]] = None) -> None:
        if storage_dir is not None:
            self.storage_dir = Path(storage_dir).resolve()
        else:
            self.storage_dir = (Path.home() / ".data-intelligence-lab" / "storage").resolve()
        self.datasets_dir = self.storage_dir / "datasets"

    def _check_path_bounds(self, target_path: Path) -> None:
        path_str = str(target_path)
        if len(path_str) > MAX_STORAGE_PATH_LENGTH:
            raise StorageError(
                f"Storage path length ({len(path_str)}) exceeds maximum supported length ({MAX_STORAGE_PATH_LENGTH})."
            )

    def save_dataset(
        self,
        dataset_id: str,
        ingestion_result: IngestionResult,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Atomically persists an IngestionResult as data.parquet and metadata.json.
        Guarantees atomic publication: writes to a staging directory on the same
        filesystem and renames it into place only once both files are fully written.

        Args:
            dataset_id: Unique, validated dataset identifier.
            ingestion_result: Result produced by core.ingest_csv().
            filename: Optional source filename (sanitized before storage).

        Returns:
            The serialized metadata dictionary written to metadata.json.

        Raises:
            InvalidDatasetIdError: If dataset_id is invalid, reserved, or attempts path traversal.
            DatasetAlreadyExistsError: If a dataset with dataset_id already exists.
            ParquetConversionError: If records cannot be serialized to Parquet (e.g. mixed offsets).
            StorageError: If disk I/O, path limits, or publication fails.
        """
        valid_id = validate_dataset_id(dataset_id)
        target_dir = self.datasets_dir / valid_id
        self._check_path_bounds(target_dir / "data.parquet")

        try:
            self.datasets_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, Exception):
            raise StorageError("Failed to initialize storage directory.") from None

        try:
            if target_dir.exists():
                raise DatasetAlreadyExistsError(valid_id) from None
        except DatasetAlreadyExistsError:
            raise
        except (OSError, Exception):
            raise StorageError(f"Failed to inspect storage for dataset '{valid_id}'.") from None

        # Create unique staging directory on the same parent directory for atomic rename
        staging_dir = self.datasets_dir / f".tmp_{uuid.uuid4().hex}_{valid_id}"
        self._check_path_bounds(staging_dir / "data.parquet")
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
        except (OSError, Exception):
            raise StorageError(f"Failed to create staging directory for dataset '{valid_id}'.") from None

        try:
            # 1. Convert to Arrow table (enforcing uniform temporal offsets)
            table = convert_to_arrow_table(ingestion_result)

            # 2. Write Parquet with snappy compression
            data_path = staging_dir / "data.parquet"
            try:
                # Flushed to disk before publication: a power loss after the rename must not
                # leave a published dataset whose Parquet bytes never reached the disk.
                with open(data_path, "wb") as data_file:
                    pq.write_table(table, data_file, compression="snappy")
                    data_file.flush()
                    os.fsync(data_file.fileno())
            except Exception:
                raise StorageError(f"Failed to write Parquet data for dataset '{valid_id}'.") from None

            # 3. Build & write metadata JSON
            metadata_dict = build_dataset_metadata(
                dataset_id=valid_id,
                ingestion_result=ingestion_result,
                filename=filename,
            )
            meta_path = staging_dir / "metadata.json"
            try:
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(metadata_dict, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                raise StorageError(f"Failed to write metadata for dataset '{valid_id}'.") from None

            # 4. Atomic directory publication
            try:
                staging_dir.replace(target_dir)
            except (FileExistsError, PermissionError):
                target_exists = False
                try:
                    target_exists = target_dir.exists()
                except Exception:
                    pass

                if target_exists:
                    raise DatasetAlreadyExistsError(valid_id) from None
                raise StorageError(f"Failed to publish dataset '{valid_id}'.") from None
            except (OSError, Exception):
                raise StorageError(f"Failed to publish dataset '{valid_id}'.") from None

            return metadata_dict

        finally:
            # Clean up staging directory if still present (best-effort, must never raise or override primary exception)
            try:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
            except Exception:
                pass

    def sweep_stale_staging(self, max_age_seconds: float = STALE_STAGING_SECONDS,
                            now: Optional[float] = None) -> int:
        """
        Removes staging directories (".tmp_<uuid>_<id>") abandoned by a process that was
        killed mid-write; its own cleanup never ran. Only directories older than
        max_age_seconds are touched, so another process's in-flight write is left alone.
        Best-effort: never raises. Returns the number removed.
        """
        removed = 0
        current = time.time() if now is None else now
        try:
            entries = list(self.datasets_dir.iterdir()) if self.datasets_dir.is_dir() else []
        except OSError:
            return 0
        for entry in entries:
            try:
                if not (entry.name.startswith(".tmp_") and entry.is_dir() and not entry.is_symlink()):
                    continue
                if current - entry.stat().st_mtime < max_age_seconds:
                    continue
                shutil.rmtree(entry)
                removed += 1
            except OSError:
                continue
        return removed

    def dataset_exists(self, dataset_id: str) -> bool:
        """
        Checks whether a valid dataset exists with both required artifacts
        (data.parquet and metadata.json).
        """
        try:
            valid_id = validate_dataset_id(dataset_id)
        except InvalidDatasetIdError:
            return False

        target_dir = self.datasets_dir / valid_id
        try:
            if not target_dir.is_dir():
                return False
            return (target_dir / "data.parquet").is_file() and (target_dir / "metadata.json").is_file()
        except Exception:
            return False

    def get_dataset_paths(self, dataset_id: str) -> DatasetPaths:
        """
        Returns the filesystem paths for a dataset, verifying its existence and integrity.

        Raises:
            InvalidDatasetIdError: If dataset_id is invalid.
            DatasetNotFoundError: If the dataset does not exist.
            DatasetCorruptError: If artifacts are missing or damaged.
            StorageError: If filesystem inspection encounters an OS error.
        """
        valid_id = validate_dataset_id(dataset_id)
        target_dir = self.datasets_dir / valid_id

        try:
            exists = target_dir.exists()
            is_dir = target_dir.is_dir() if exists else False
        except (OSError, Exception):
            raise StorageError(f"Failed to access storage for dataset '{valid_id}'.") from None

        if not exists:
            raise DatasetNotFoundError(valid_id)
        if not is_dir:
            raise DatasetCorruptError(valid_id, "Target path is not a directory.")

        data_path = target_dir / "data.parquet"
        meta_path = target_dir / "metadata.json"

        try:
            has_data = data_path.is_file()
            has_meta = meta_path.is_file()
        except (OSError, Exception):
            raise StorageError(f"Failed to inspect artifacts for dataset '{valid_id}'.") from None

        if not has_data:
            raise DatasetCorruptError(valid_id, "Missing 'data.parquet' artifact.")
        if not has_meta:
            raise DatasetCorruptError(valid_id, "Missing 'metadata.json' artifact.")

        return DatasetPaths(
            dataset_id=valid_id,
            dataset_dir=target_dir,
            data_path=data_path,
            metadata_path=meta_path,
        )

    def load_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """
        Loads and parses metadata.json for a dataset.

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            DatasetCorruptError: If metadata cannot be read or parsed, or contains invalid encoding.
            StorageError: If disk read fails.
        """
        paths = self.get_dataset_paths(dataset_id)
        try:
            with open(paths.metadata_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except UnicodeError:
            raise DatasetCorruptError(
                paths.dataset_id, "Metadata artifact contains invalid UTF-8 encoding."
            ) from None
        except json.JSONDecodeError as err:
            raise DatasetCorruptError(
                paths.dataset_id, f"Invalid JSON in metadata (line {err.lineno})."
            ) from None
        except (OSError, Exception):
            raise StorageError(f"Failed to read metadata for dataset '{paths.dataset_id}'.") from None

    def read_parquet_table(self, dataset_id: str) -> pa.Table:
        """
        Reads the dataset's data.parquet file as a PyArrow Table.

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            DatasetCorruptError: If the Parquet file is corrupted or unreadable.
            StorageError: If disk read fails.
        """
        paths = self.get_dataset_paths(dataset_id)
        try:
            return pq.read_table(paths.data_path)
        except Exception:
            # Redact raw PyArrow exception message to prevent leaking absolute filesystem paths
            raise DatasetCorruptError(
                paths.dataset_id, "Parquet data artifact is corrupted or unreadable."
            ) from None

    def delete_dataset(self, dataset_id: str) -> bool:
        """
        Deletes a dataset directory if present.
        Returns True if deleted, False if not found.
        """
        valid_id = validate_dataset_id(dataset_id)
        target_dir = self.datasets_dir / valid_id
        try:
            if not target_dir.exists():
                return False
            shutil.rmtree(target_dir)
            return True
        except (OSError, Exception):
            raise StorageError(f"Failed to delete dataset '{valid_id}'.") from None


# Architectural alias for LocalExecutionAdapter compatibility
LocalStorageAdapter = LocalDatasetStorage
