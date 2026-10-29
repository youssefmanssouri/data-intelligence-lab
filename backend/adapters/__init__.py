"""
Data Intelligence Lab - Backend Adapters Package
Exposes execution and persistence adapters conforming to the Hexagonal Architecture.
"""

from backend.adapters.exceptions import (
    DatasetAlreadyExistsError,
    DatasetCorruptError,
    DatasetNotFoundError,
    InvalidDatasetIdError,
    ParquetConversionError,
    StorageError,
)
from backend.adapters.local_execution import LocalExecutionAdapter
from backend.adapters.local_storage import (
    DatasetPaths,
    LocalDatasetStorage,
    LocalStorageAdapter,
    build_dataset_metadata,
    convert_to_arrow_table,
    format_timezone_offset,
    sanitize_filename,
    serialize_value,
    validate_dataset_id,
)

__all__ = [
    "DatasetAlreadyExistsError",
    "DatasetCorruptError",
    "DatasetNotFoundError",
    "DatasetPaths",
    "InvalidDatasetIdError",
    "LocalDatasetStorage",
    "LocalExecutionAdapter",
    "LocalStorageAdapter",
    "ParquetConversionError",
    "StorageError",
    "build_dataset_metadata",
    "convert_to_arrow_table",
    "format_timezone_offset",
    "sanitize_filename",
    "serialize_value",
    "validate_dataset_id",
]
