"""
Data Intelligence Lab - Storage and Adapter Exceptions
Defines sanitized exceptions for dataset persistence and local adapter boundaries.
"""

from typing import Optional


class StorageError(Exception):
    """Base exception for dataset persistence and adapter failures."""

    def __init__(self, message: str, code: str = "ERR_STORAGE_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class DatasetNotFoundError(StorageError):
    """Raised when a requested dataset identifier does not exist in storage."""

    def __init__(self, dataset_id: str) -> None:
        msg = f"Dataset '{dataset_id}' not found."
        super().__init__(msg, code="ERR_DATASET_NOT_FOUND")
        self.dataset_id = dataset_id


class DatasetAlreadyExistsError(StorageError):
    """Raised when attempting to persist a dataset with an identifier that already exists."""

    def __init__(self, dataset_id: str) -> None:
        msg = f"Dataset '{dataset_id}' already exists in storage."
        super().__init__(msg, code="ERR_DATASET_ALREADY_EXISTS")
        self.dataset_id = dataset_id


class InvalidDatasetIdError(StorageError):
    """Raised when a dataset identifier is malformed, empty, or attempts path traversal."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="ERR_INVALID_DATASET_ID")


class DatasetCorruptError(StorageError):
    """Raised when a dataset directory exists but required artifacts are missing or unreadable."""

    def __init__(self, dataset_id: str, reason: str) -> None:
        msg = f"Dataset '{dataset_id}' is corrupt: {reason}"
        super().__init__(msg, code="ERR_DATASET_CORRUPT")
        self.dataset_id = dataset_id
        self.reason = reason


class ParquetConversionError(StorageError):
    """Raised when converting dataset records to Apache Arrow / Parquet format fails."""

    def __init__(self, message: str, column_name: Optional[str] = None) -> None:
        super().__init__(message, code="ERR_PARQUET_CONVERSION")
        self.column_name = column_name
