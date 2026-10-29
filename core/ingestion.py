"""
Data Intelligence Lab - Core CSV Ingestion Orchestrator
Streams and validates raw CSV bytes, enforces security boundaries, and outputs typed profiles.
"""

from __future__ import annotations
import csv
import io
import os
from pathlib import Path
import time
from typing import Any, BinaryIO, Dict, List, Sequence, Tuple, Union

from .detection import detect_and_decode_encoding, detect_delimiter, detect_encoding
from .profiling import profile_dataset
from .sanitization import normalize_headers
from .type_inference import infer_column_type
from .types import (
    CellSizeLimitExceededError,
    ColumnCountLimitExceededError,
    ColumnMetadata,
    EmptyDatasetError,
    ExecutionDeadline,
    FileSizeLimitExceededError,
    HeaderValidationError,
    InferencePolicy,
    IngestionLimits,
    IngestionResult,
    MalformedCSVError,
    ParserFieldSizeLimitError,
    ProcessingTimeoutError,
    RowCountLimitExceededError,
    SourceReadError,
)


def _read_bounded_chunks(
    stream: BinaryIO,
    max_bytes: int,
    deadline: Optional[ExecutionDeadline] = None,
) -> Tuple[bytes, int]:
    """
    Reads a binary stream in fixed chunks up to max_bytes.
    Halts reading immediately and raises FileSizeLimitExceededError if the stream exceeds max_bytes.
    """
    buffer = bytearray()
    chunk_size = 64 * 1024  # 64 KB chunks
    total_bytes = 0

    while True:
        if deadline is not None:
            deadline.check()
        try:
            chunk = stream.read(chunk_size)
        except (OSError, IOError):
            raise SourceReadError("Failed to read from source stream: I/O error.") from None
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise FileSizeLimitExceededError(actual_bytes=total_bytes, max_bytes=max_bytes)
        buffer.extend(chunk)

    return bytes(buffer), total_bytes


def _read_and_validate_stream(
    source: Union[bytes, BinaryIO, Path, str],
    max_bytes: int,
    deadline: Optional[ExecutionDeadline] = None,
) -> Tuple[bytes, int]:
    """
    Reads incoming binary source (bytes, binary stream, or file path) safely,
    enforcing max_bytes without buffering unbounded streams or relying on pre-read size checks.
    """
    if isinstance(source, bytes):
        total_bytes = len(source)
        if total_bytes > max_bytes:
            raise FileSizeLimitExceededError(actual_bytes=total_bytes, max_bytes=max_bytes)
        if deadline is not None:
            deadline.check()
        return source, total_bytes

    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            is_file = path.is_file()
        except FileNotFoundError:
            raise FileNotFoundError(f"Source file not found: {path.name}")
        except OSError:
            raise SourceReadError(f"Failed to access source file '{path.name}': permission denied or I/O error.") from None

        if not is_file:
            raise FileNotFoundError(f"Source file not found: {path.name}")

        # Fast preliminary rejection for files already known to exceed max_bytes
        try:
            file_size = os.path.getsize(path)
            if file_size > max_bytes:
                raise FileSizeLimitExceededError(actual_bytes=file_size, max_bytes=max_bytes)
        except OSError:
            pass
        # Always open and read via bounded chunk reader to prevent TOCTOU bypasses
        try:
            with open(path, "rb") as f:
                return _read_bounded_chunks(f, max_bytes=max_bytes, deadline=deadline)
        except FileNotFoundError:
            raise FileNotFoundError(f"Source file not found: {path.name}")
        except (PermissionError, OSError):
            raise SourceReadError(f"Failed to read source file '{path.name}': permission denied or I/O error.") from None

    # File-like object (BinaryIO)
    return _read_bounded_chunks(source, max_bytes=max_bytes, deadline=deadline)


def ingest_csv(
    source: Union[bytes, BinaryIO, Path, str],
    limits: IngestionLimits = IngestionLimits(),
    *,
    inference_policy: Optional[InferencePolicy] = None,
) -> IngestionResult:
    """
    Primary analytical core entrypoint for ingesting and profiling a CSV dataset.

    Parameters:
        source: Raw bytes, binary file-like stream, or file path.
        limits: Configurable boundaries (max_bytes, max_rows, max_columns, max_cell_bytes, max_processing_seconds).
        inference_policy: Optional explicit policy governing type inference and literal columns.

    Returns:
        IngestionResult: Contains complete DatasetProfile, ColumnMetadata list, and typed records.

    Raises:
        FileSizeLimitExceededError: If input exceeds limits.max_bytes.
        CellSizeLimitExceededError: If any single cell exceeds limits.max_cell_bytes.
        RowCountLimitExceededError: If data rows exceed limits.max_rows.
        ColumnCountLimitExceededError: If columns exceed limits.max_columns.
        ProcessingTimeoutError: If execution exceeds limits.max_processing_seconds.
        EmptyDatasetError: If dataset contains 0 bytes or only whitespace.
        MalformedCSVError: If CSV rows are structurally ragged or syntactically malformed.
        EncodingDetectionError: If character encoding cannot be decoded deterministically.
        DelimiterDetectionError: If delimiter cannot be detected or is ambiguous.
        SourceReadError: If reading source file or stream encounters an I/O or permission error.
    """
    # Validate configurations before source acquisition
    if not isinstance(limits, IngestionLimits):
        raise TypeError(f"limits must be an instance of IngestionLimits, got {type(limits).__name__}.")
    if inference_policy is not None:
        if not isinstance(inference_policy, InferencePolicy):
            raise TypeError(
                f"inference_policy must be an instance of InferencePolicy, got {type(inference_policy).__name__}."
            )
        for col_name in inference_policy.preserve_as_text:
            if not isinstance(col_name, str):
                raise TypeError("All column names in preserve_as_text must be strings.")
            if not col_name.strip():
                raise ValueError("Column names in preserve_as_text cannot be empty.")
    policy = inference_policy if inference_policy is not None else InferencePolicy()

    deadline = ExecutionDeadline(limits.max_processing_seconds)

    # 1. Read binary stream with strict byte limit and deadline tracking
    raw_bytes, total_bytes = _read_and_validate_stream(source, limits.max_bytes, deadline=deadline)
    if total_bytes == 0 or not raw_bytes.strip():
        raise EmptyDatasetError()

    deadline.check()

    # 2. Coordinated encoding selection and decoding over complete bounded input
    encoding, decoded_text = detect_and_decode_encoding(raw_bytes, deadline=deadline)

    # Free raw bytes immediately to prevent retaining duplicate raw/decoded representations
    del raw_bytes

    deadline.check()

    # 3. Delimiter detection using logical-record CSV parsing on explicit newline-preserving stream
    text_stream = io.StringIO(decoded_text, newline="")
    delimiter = detect_delimiter(text_stream, deadline=deadline)
    text_stream.seek(0)

    deadline.check()

    # 4. Stream and parse rows using csv.reader with strict parser settings
    reader = csv.reader(
        text_stream,
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
        escapechar=None,
        skipinitialspace=False,
        strict=True,
    )

    # Read Header
    logical_record = 1
    try:
        raw_headers = next(reader)
    except StopIteration:
        raise EmptyDatasetError()
    except csv.Error as e:
        line_num = getattr(reader, "line_num", None)
        if "field larger than field limit" in str(e).lower():
            raise ParserFieldSizeLimitError(row_number=1) from None
        raise MalformedCSVError(
            row_number=1,
            message="Malformed CSV at row 1: syntax error while parsing header.",
            line_number=line_num,
        ) from None

    if not raw_headers or all(h.strip() == "" for h in raw_headers):
        raise HeaderValidationError("First row is completely empty or lacks valid headers.")

    expected_cols = len(raw_headers)
    if expected_cols > limits.max_columns:
        raise ColumnCountLimitExceededError(actual_columns=expected_cols, max_columns=limits.max_columns)

    # Check header cell sizes
    for c_idx, h in enumerate(raw_headers):
        cell_bytes = len(h.encode("utf-8"))
        if cell_bytes > limits.max_cell_bytes:
            raise CellSizeLimitExceededError(
                row_number=1,
                column_index=c_idx,
                actual_bytes=cell_bytes,
                max_bytes=limits.max_cell_bytes,
            )

    # Normalize headers
    normalized_pairs = normalize_headers(raw_headers)
    column_names = [pair[0] for pair in normalized_pairs]

    # Validate that preserve_as_text names belong to the normalized column set
    if not policy.preserve_as_text.issubset(set(column_names)):
        raise ValueError("Unknown normalized column in preserve_as_text.")

    # Pre-allocate column data buffers: normalized_name -> list of raw string values
    columns_raw: Dict[str, List[str]] = {name: [] for name in column_names}
    seen_row_hashes: Set[int] = set()
    duplicate_rows = 0
    row_count = 0

    # 5. Read and validate data rows (tracking logical record numbers independently from physical lines)
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as e:
            failing_record = logical_record + 1
            line_num = getattr(reader, "line_num", None)
            if "field larger than field limit" in str(e).lower():
                raise ParserFieldSizeLimitError(row_number=failing_record) from None
            raise MalformedCSVError(
                row_number=failing_record,
                message=f"Malformed CSV at row {failing_record}: syntax error.",
                line_number=line_num,
            ) from None

        logical_record += 1

        # Cooperative deadline check every 100 rows
        if logical_record % 100 == 0:
            deadline.check()

        # Check for ragged rows
        actual_cols = len(row)
        if actual_cols != expected_cols:
            raise MalformedCSVError(
                row_number=logical_record,
                expected_columns=expected_cols,
                actual_columns=actual_cols,
            )

        # Row limit check
        row_count += 1
        if row_count > limits.max_rows:
            raise RowCountLimitExceededError(actual_rows=row_count, max_rows=limits.max_rows)

        # Validate cell sizes and buffer values
        for c_idx, cell_value in enumerate(row):
            cell_bytes = len(cell_value.encode("utf-8"))
            if cell_bytes > limits.max_cell_bytes:
                raise CellSizeLimitExceededError(
                    row_number=logical_record,
                    column_index=c_idx,
                    actual_bytes=cell_bytes,
                    max_bytes=limits.max_cell_bytes,
                )
            columns_raw[column_names[c_idx]].append(cell_value)

        # Running hash set for duplicate row counting (avoids storing duplicate row_hashes lists)
        row_hash = hash(tuple(row))
        if row_hash in seen_row_hashes:
            duplicate_rows += 1
        else:
            seen_row_hashes.add(row_hash)

    # P1-01: Close and release the text stream and exhausted csv.reader immediately after row parsing
    text_stream.close()
    del reader, text_stream, decoded_text, seen_row_hashes

    deadline.check()

    # 6. Type inference per column
    columns_data: Dict[str, Tuple[str, List[Any], int]] = {}
    column_metadata: List[ColumnMetadata] = []

    for idx, (norm_name, raw_name) in enumerate(normalized_pairs):
        deadline.check()
        # Free raw string column list as soon as typed values are inferred
        raw_col = columns_raw.pop(norm_name)
        is_literal = norm_name in policy.preserve_as_text
        res = infer_column_type(raw_col, deadline=deadline, is_literal=is_literal)
        del raw_col

        columns_data[norm_name] = res
        column_metadata.append(
            ColumnMetadata(
                index=idx,
                raw_name=raw_name,
                normalized_name=norm_name,
                inferred_type=res.inferred_type,
                inference_policy_version="1.0",
                inference_reason=res.inference_reason,
            )
        )

    deadline.check()

    # 7. Generate full DatasetProfile
    profile = profile_dataset(
        row_count=row_count,
        column_metadata=column_metadata,
        columns_data=columns_data,
        encoding=encoding,
        delimiter=delimiter,
        duplicate_rows=duplicate_rows,
        deadline=deadline,
    )

    deadline.check()

    # 8. Build normalized records (row-oriented dictionaries)
    records: List[Dict[str, Any]] = []
    for r_idx in range(row_count):
        if r_idx % 100 == 0:
            deadline.check()
        record: Dict[str, Any] = {}
        for norm_name in column_names:
            record[norm_name] = columns_data[norm_name][1][r_idx]
        records.append(record)

    # P1-02: Check deadline immediately after record assembly completes
    deadline.check()

    # P1-02: Check deadline immediately before returning IngestionResult
    result = IngestionResult(
        profile=profile,
        column_metadata=column_metadata,
        records=records,
    )
    deadline.check()
    return result
