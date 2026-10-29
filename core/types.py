"""
Data Intelligence Lab - Core Types and Models
Defines immutable data models, configuration limits, and domain exceptions.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


import math
import time

# =====================================================================
# Ingestion & Resource Limits
# =====================================================================

_DEFAULT_MAX_CELL_BYTES: int = 32 * 1024

@dataclass(frozen=True)
class IngestionLimits:
    """Configurable boundaries for CSV ingestion and resource consumption."""
    max_bytes: int = 200 * 1024 * 1024        # 200 MB hard ceiling
    max_rows: int = 1_000_000                  # 1,000,000 rows ceiling
    max_columns: int = 1_000                   # 1,000 columns ceiling
    max_cell_bytes: Optional[int] = None       # Defaults to min(32 KB, max_bytes)
    max_processing_seconds: float = 30.0       # Processing timeout ceiling

    def __post_init__(self) -> None:
        """Validates configuration values at runtime, failing fast on invalid inputs."""
        # 1. max_bytes validation
        if type(self.max_bytes) is not int:
            raise TypeError(f"max_bytes must be an integer, got {type(self.max_bytes).__name__}.")
        if self.max_bytes <= 0:
            raise ValueError(f"max_bytes must be a positive integer (> 0), got {self.max_bytes}.")

        # 2. max_rows validation
        if type(self.max_rows) is not int:
            raise TypeError(f"max_rows must be an integer, got {type(self.max_rows).__name__}.")
        if self.max_rows <= 0:
            raise ValueError(f"max_rows must be a positive integer (> 0), got {self.max_rows}.")

        # 3. max_columns validation
        if type(self.max_columns) is not int:
            raise TypeError(f"max_columns must be an integer, got {type(self.max_columns).__name__}.")
        if self.max_columns <= 0:
            raise ValueError(f"max_columns must be a positive integer (> 0), got {self.max_columns}.")

        # 4. max_cell_bytes validation & smart defaulting
        if self.max_cell_bytes is None:
            object.__setattr__(self, "max_cell_bytes", min(_DEFAULT_MAX_CELL_BYTES, self.max_bytes))
        else:
            if type(self.max_cell_bytes) is not int:
                raise TypeError(f"max_cell_bytes must be an integer, got {type(self.max_cell_bytes).__name__}.")
            if self.max_cell_bytes <= 0:
                raise ValueError(f"max_cell_bytes must be a positive integer (> 0), got {self.max_cell_bytes}.")
            if self.max_cell_bytes > self.max_bytes:
                raise ValueError(
                    f"max_cell_bytes ({self.max_cell_bytes:,} bytes) cannot exceed max_bytes ({self.max_bytes:,} bytes)."
                )

        # 5. max_processing_seconds validation
        if type(self.max_processing_seconds) not in (int, float) or isinstance(self.max_processing_seconds, bool):
            raise TypeError(
                f"max_processing_seconds must be a float or integer, got {type(self.max_processing_seconds).__name__}."
            )
        if math.isnan(self.max_processing_seconds) or math.isinf(self.max_processing_seconds):
            raise ValueError(
                f"max_processing_seconds must be a finite positive number, got {self.max_processing_seconds}."
            )
        if self.max_processing_seconds <= 0:
            raise ValueError(
                f"max_processing_seconds must be greater than zero (> 0), got {self.max_processing_seconds}."
            )


class ExecutionDeadline:
    """
    Cooperative processing deadline tracker.
    Checks elapsed monotonic time against a configured timeout duration.
    """
    def __init__(self, timeout_seconds: float, start_time: Optional[float] = None) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.start_time = time.monotonic() if start_time is None else float(start_time)
        self.deadline = self.start_time + self.timeout_seconds

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def is_exceeded(self) -> bool:
        return time.monotonic() > self.deadline

    def check(self) -> None:
        """Raises ProcessingTimeoutError if the current monotonic time exceeds the deadline."""
        now = time.monotonic()
        if now > self.deadline:
            raise ProcessingTimeoutError(
                elapsed_seconds=now - self.start_time,
                max_seconds=self.timeout_seconds,
            )


@dataclass(frozen=True)
class InferencePolicy:
    """Explicit configuration policy governing type inference and literal column preservation."""
    preserve_as_text: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Validates preserve_as_text at runtime, ensuring frozen set of strings."""
        if not isinstance(self.preserve_as_text, (frozenset, set, list, tuple)):
            raise TypeError(
                f"preserve_as_text must be a frozenset or collection of strings, got {type(self.preserve_as_text).__name__}."
            )
        for item in self.preserve_as_text:
            if not isinstance(item, str):
                raise TypeError(
                    f"All column names in preserve_as_text must be strings, got {type(item).__name__}."
                )
            if not item.strip():
                raise ValueError("Column names in preserve_as_text cannot be empty.")
        object.__setattr__(self, "preserve_as_text", frozenset(self.preserve_as_text))


class InferenceResult(tuple):
    """
    Three-item tuple (inferred_type, typed_values, null_count) preserving 100% backward
    compatibility with existing callers while exposing .inference_reason.
    """
    def __new__(
        cls,
        inferred_type: str,
        typed_values: List[Any],
        null_count: int,
        inference_reason: str = "DEFAULT",
    ) -> InferenceResult:
        obj = super().__new__(cls, (inferred_type, typed_values, null_count))
        obj.inferred_type = inferred_type
        obj.typed_values = typed_values
        obj.null_count = null_count
        obj.inference_reason = inference_reason
        return obj

    def __repr__(self) -> str:
        return (
            f"InferenceResult(inferred_type={self.inferred_type!r}, "
            f"typed_values=<{len(self.typed_values)} items>, "
            f"null_count={self.null_count}, "
            f"inference_reason={self.inference_reason!r})"
        )


# =====================================================================
# Inference Decision Codes
# =====================================================================

DECISION_PRESERVE_AS_TEXT = "PRESERVE_AS_TEXT"
DECISION_ALL_NULL = "ALL_NULL"
DECISION_BOOLEAN = "BOOLEAN"
DECISION_INTEGER = "INTEGER"
DECISION_FLOAT = "FLOAT"
DECISION_DATE = "DATE"
DECISION_DATETIME = "DATETIME"
DECISION_LEADING_ZERO = "LEADING_ZERO"
DECISION_FLOAT_PRECISION_LOSS = "FLOAT_PRECISION_LOSS"
DECISION_FLOAT_UNDERFLOW = "FLOAT_UNDERFLOW"
DECISION_NON_FINITE_FLOAT = "NON_FINITE_FLOAT"
DECISION_MIXED_AWARENESS = "MIXED_AWARENESS"
DECISION_MIXED_TEMPORAL_KINDS = "MIXED_TEMPORAL_KINDS"
DECISION_SUBMICROSECOND_PRECISION = "SUBMICROSECOND_PRECISION"
DECISION_NO_UNIFORM_TYPE = "NO_UNIFORM_TYPE"

INFERENCE_DECISION_CODES = frozenset({
    DECISION_PRESERVE_AS_TEXT,
    DECISION_ALL_NULL,
    DECISION_BOOLEAN,
    DECISION_INTEGER,
    DECISION_FLOAT,
    DECISION_DATE,
    DECISION_DATETIME,
    DECISION_LEADING_ZERO,
    DECISION_FLOAT_PRECISION_LOSS,
    DECISION_FLOAT_UNDERFLOW,
    DECISION_NON_FINITE_FLOAT,
    DECISION_MIXED_AWARENESS,
    DECISION_MIXED_TEMPORAL_KINDS,
    DECISION_SUBMICROSECOND_PRECISION,
    DECISION_NO_UNIFORM_TYPE,
})


# =====================================================================
# Profiling & Metadata Models
# =====================================================================

@dataclass(frozen=True)
class ColumnMetadata:
    """Metadata describing a single column's identification, naming, and inference decision."""
    index: int
    raw_name: str
    normalized_name: str
    inferred_type: str
    inference_policy_version: str = "1.0"
    inference_reason: str = "DEFAULT"


@dataclass(frozen=True)
class ColumnProfile:
    """Comprehensive statistical and structural profile of a single column."""
    index: int
    name: str
    raw_name: str
    inferred_type: str
    total_count: int
    null_count: int
    null_ratio: float
    distinct_count: int
    is_unique: bool
    sample_values: List[Any] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataQualityReport:
    """Deterministic data quality scoring and anomaly flags."""
    overall_score: float             # 0.0 to 100.0
    completeness_score: float        # 0.0 to 100.0
    uniqueness_score: float          # 0.0 to 100.0
    validity_score: float            # 0.0 to 100.0
    total_cells: int
    total_missing_cells: int
    duplicate_row_count: int
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DatasetProfile:
    """Complete profile of an ingested dataset."""
    row_count: int
    column_count: int
    encoding: str
    delimiter: str
    columns: List[ColumnProfile]
    quality: DataQualityReport
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class IngestionResult:
    """Output structure returned upon successful ingestion and profiling."""
    profile: DatasetProfile
    column_metadata: List[ColumnMetadata]
    records: List[Dict[str, Any]]     # Normalized row records (for downstream serialization / Parquet)


# =====================================================================
# Domain Exceptions (Safe, Redacted)
# =====================================================================

class IngestionError(Exception):
    """Base exception for all ingestion failures. Sanitizes internal paths."""
    def __init__(self, message: str, code: str = "ERR_INGESTION_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class FileSizeLimitExceededError(IngestionError):
    """Raised when incoming dataset exceeds max_bytes."""
    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        msg = f"Dataset size ({actual_bytes:,} bytes) exceeds configured limit ({max_bytes:,} bytes)."
        super().__init__(msg, code="ERR_FILE_SIZE_LIMIT_EXCEEDED")
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes


class CellSizeLimitExceededError(IngestionError):
    """Raised when an individual cell exceeds max_cell_bytes (expansion bomb protection) or native parser limit."""
    def __init__(
        self,
        row_number: int,
        column_index: Optional[int] = None,
        actual_bytes: Optional[int] = None,
        max_bytes: Optional[int] = None,
        message: Optional[str] = None,
        *,
        is_parser_limit: bool = False,
    ) -> None:
        if message is not None:
            msg = message
        elif is_parser_limit:
            msg = f"Cell at row {row_number} exceeds native CSV parser field size limit."
        elif column_index is not None and actual_bytes is not None and max_bytes is not None:
            msg = (
                f"Cell at row {row_number}, column {column_index} length ({actual_bytes:,} bytes) "
                f"exceeds maximum allowed cell size ({max_bytes:,} bytes)."
            )
        elif max_bytes is not None:
            msg = f"Cell at row {row_number} exceeds maximum allowed cell size ({max_bytes:,} bytes)."
        else:
            msg = f"Cell at row {row_number} exceeds maximum allowed cell size."
        super().__init__(msg, code="ERR_CELL_SIZE_LIMIT_EXCEEDED")
        self.row_number = row_number
        self.column_index = column_index
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        self.is_parser_limit = is_parser_limit


class ParserFieldSizeLimitError(CellSizeLimitExceededError):
    """Raised when an individual field exceeds the Python CSV native parser field size limit."""
    def __init__(self, row_number: int, message: Optional[str] = None) -> None:
        msg = message or f"Cell at row {row_number} exceeds native CSV parser field size limit."
        super().__init__(
            row_number=row_number,
            column_index=None,
            actual_bytes=None,
            max_bytes=None,
            message=msg,
            is_parser_limit=True,
        )


class RowCountLimitExceededError(IngestionError):
    """Raised when total data rows exceed max_rows."""
    def __init__(self, actual_rows: int, max_rows: int) -> None:
        msg = f"Dataset row count ({actual_rows:,}) exceeds configured limit ({max_rows:,})."
        super().__init__(msg, code="ERR_ROW_COUNT_LIMIT_EXCEEDED")
        self.actual_rows = actual_rows
        self.max_rows = max_rows


class ColumnCountLimitExceededError(IngestionError):
    """Raised when column count exceeds max_columns."""
    def __init__(self, actual_columns: int, max_columns: int) -> None:
        msg = f"Dataset column count ({actual_columns:,}) exceeds configured limit ({max_columns:,})."
        super().__init__(msg, code="ERR_COLUMN_COUNT_LIMIT_EXCEEDED")
        self.actual_columns = actual_columns
        self.max_columns = max_columns


class ProcessingTimeoutError(IngestionError):
    """Raised when processing time exceeds max_processing_seconds."""
    def __init__(self, elapsed_seconds: float, max_seconds: float) -> None:
        msg = f"Ingestion timed out after {elapsed_seconds:.1f}s (configured limit: {max_seconds:.1f}s)."
        super().__init__(msg, code="ERR_PROCESSING_TIMEOUT")
        self.elapsed_seconds = elapsed_seconds
        self.max_seconds = max_seconds


class EncodingDetectionError(IngestionError):
    """Raised when file encoding cannot be determined or decoded deterministically."""
    def __init__(self, reason: str) -> None:
        super().__init__(f"Character encoding detection failed: {reason}", code="ERR_ENCODING_DETECTION")


class DelimiterDetectionError(IngestionError):
    """Raised when delimiter cannot be detected or is ambiguous."""
    def __init__(self, reason: str) -> None:
        super().__init__(f"Delimiter detection failed: {reason}", code="ERR_DELIMITER_DETECTION")


class MalformedCSVError(IngestionError):
    """Raised when CSV structure is syntactically invalid (ragged rows, unclosed quotes)."""
    def __init__(
        self,
        row_number: Optional[int] = None,
        expected_columns: Optional[int] = None,
        actual_columns: Optional[int] = None,
        message: Optional[str] = None,
        *,
        line_number: Optional[int] = None,
    ) -> None:
        if message is not None:
            msg = message
        elif expected_columns is not None and actual_columns is not None and row_number is not None:
            msg = (
                f"Malformed CSV at row {row_number}: expected {expected_columns} columns, "
                f"but found {actual_columns} columns."
            )
        elif row_number is not None:
            msg = f"Malformed CSV syntax at row {row_number}."
        else:
            msg = "Malformed CSV syntax."
        super().__init__(msg, code="ERR_MALFORMED_CSV")
        self.row_number = row_number
        self.expected_columns = expected_columns
        self.actual_columns = actual_columns
        self.line_number = line_number


class EmptyDatasetError(IngestionError):
    """Raised when input stream contains 0 bytes or only whitespace."""
    def __init__(self) -> None:
        super().__init__("The provided dataset is empty.", code="ERR_EMPTY_DATASET")


class HeaderValidationError(IngestionError):
    """Raised when CSV headers are invalid or completely empty."""
    def __init__(self, detail: str) -> None:
        super().__init__(f"Header validation error: {detail}", code="ERR_HEADER_VALIDATION")


class SourceReadError(IngestionError):
    """Raised when opening or reading the source input fails due to I/O or permission errors."""
    def __init__(self, message: str) -> None:
        super().__init__(message, code="ERR_SOURCE_READ")


class NumericRangeError(IngestionError):
    """Raised when a numeric calculation exceeds representation limits (overflow/non-finite)."""
    def __init__(
        self,
        operation: str,
        column_index: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        if message is not None:
            msg = message
        elif column_index is not None:
            msg = f"Numeric calculation exceeded representation limits in column {column_index} during '{operation}'."
        else:
            msg = f"Numeric calculation exceeded representation limits during '{operation}'."
        super().__init__(msg, code="ERR_NUMERIC_RANGE")
        self.operation = operation
        self.column_index = column_index

