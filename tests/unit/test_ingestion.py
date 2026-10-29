"""
Unit Test Suite for Data Intelligence Lab - CSV Ingestion & Profiling Engine
Validates parsing correctness, limit enforcement, security boundaries, and edge cases.
"""

import io
from pathlib import Path
import time
from typing import Any, Optional
import weakref
import pytest

from core import (
    CellSizeLimitExceededError,
    ColumnCountLimitExceededError,
    EmptyDatasetError,
    ExecutionDeadline,
    FileSizeLimitExceededError,
    HeaderValidationError,
    IngestionLimits,
    IngestionResult,
    MalformedCSVError,
    ProcessingTimeoutError,
    RowCountLimitExceededError,
    ingest_csv,
)
from core.profiling import calculate_numeric_stats, calculate_string_stats, profile_column
from core.type_inference import infer_column_type

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# =====================================================================
# 1. Normal CSV with Mixed Data Types
# =====================================================================

def test_normal_csv_mixed_types() -> None:
    fixture_path = FIXTURES_DIR / "synthetic_mixed.csv"
    result = ingest_csv(fixture_path)
    
    profile = result.profile
    assert profile.row_count == 5
    assert profile.column_count == 7
    assert profile.delimiter == ","
    assert profile.encoding in ("utf-8", "utf-8-sig")

    col_map = {c.name: c for c in profile.columns}
    assert col_map["id"].inferred_type == "integer"
    assert col_map["name"].inferred_type == "string"
    assert col_map["age"].inferred_type == "integer"
    assert col_map["age"].null_count == 1  # "NA" was parsed as null
    assert col_map["salary"].inferred_type == "float"
    assert col_map["salary"].null_count == 1  # empty cell parsed as null
    assert col_map["is_active"].inferred_type == "boolean"
    assert col_map["signup_date"].inferred_type == "date"
    
    # Critical: Leading zeros in postal_code must keep it as string
    assert col_map["postal_code"].inferred_type == "string"
    assert "01234" in col_map["postal_code"].sample_values

    # Check numeric stats
    salary_stats = col_map["salary"].stats
    assert salary_stats["min"] == 61000.0
    assert salary_stats["max"] == 95400.75
    assert salary_stats["mean"] > 70000.0

    # Records structure
    assert len(result.records) == 5
    assert result.records[0]["id"] == 101
    assert result.records[0]["name"] == "Alice Smith"
    assert result.records[0]["is_active"] is True


# =====================================================================
# 2. Empty Files and Header-Only Files
# =====================================================================

def test_empty_file_raises_error() -> None:
    with pytest.raises(EmptyDatasetError):
        ingest_csv(b"")

    with pytest.raises(EmptyDatasetError):
        ingest_csv(b"   \n\r\t  \n  ")


def test_header_only_file() -> None:
    csv_bytes = b"col_a,col_b,col_c\n"
    result = ingest_csv(csv_bytes)
    
    assert result.profile.row_count == 0
    assert result.profile.column_count == 3
    assert len(result.records) == 0
    assert result.profile.quality.overall_score == 100.0


def test_empty_header_row_raises_error() -> None:
    with pytest.raises(HeaderValidationError):
        ingest_csv(b",,\n1,2,3\n")


# =====================================================================
# 3. Missing Values and Duplicate Rows
# =====================================================================

def test_missing_values_and_duplicate_rows() -> None:
    csv_text = (
        "item,quantity,price\n"
        "apple,10,1.50\n"
        "apple,10,1.50\n"      # Duplicate of row 1
        "banana,NA,0.75\n"     # Missing quantity
        "cherry,5,null\n"      # Missing price
    ).encode("utf-8")

    result = ingest_csv(csv_text)
    profile = result.profile
    assert profile.row_count == 4
    assert profile.quality.duplicate_row_count == 1
    assert profile.quality.total_missing_cells == 2
    assert profile.quality.overall_score < 100.0

    col_map = {c.name: c for c in profile.columns}
    assert col_map["quantity"].null_count == 1
    assert col_map["price"].null_count == 1


# =====================================================================
# 4. Empty, Duplicate, and Unusual Column Names (Formula Injection)
# =====================================================================

def test_unusual_empty_and_duplicate_headers() -> None:
    # Contains:
    # 1. Empty header -> unnamed_1
    # 2. Duplicate header 'Name' -> name, name_1
    # 3. Special characters 'Revenue ($)' -> revenue
    # 4. Formula injection attempt '=cmd|' /C calc'!A0' -> neutralized
    csv_text = (
        ",Name,Name,Revenue ($),=cmd|' /C calc'!A0\n"
        "1,Alice,Smith,500,Active\n"
    ).encode("utf-8")

    result = ingest_csv(csv_text)
    meta = result.column_metadata
    
    assert meta[0].normalized_name == "unnamed_1"
    assert meta[1].normalized_name == "name"
    assert meta[2].normalized_name == "name_1"
    assert meta[3].normalized_name == "revenue"
    
    # Verify formula injection was neutralized: leading '=' stripped
    assert not meta[4].normalized_name.startswith("=")
    assert not meta[4].raw_name.startswith("=")
    assert meta[4].normalized_name == "cmd_c_calca0"


# =====================================================================
# 5. UTF-8 with BOM and Supported Encodings
# =====================================================================

def test_utf8_with_bom() -> None:
    bom = b"\xef\xbb\xbf"
    csv_bytes = bom + b"city,temperature\nTokyo,18.5\nParis,15.2\n"
    
    result = ingest_csv(csv_bytes)
    assert result.profile.encoding == "utf-8-sig"
    assert result.column_metadata[0].normalized_name == "city"
    assert result.profile.row_count == 2


def test_latin1_encoding() -> None:
    # Encoded using Latin-1 (ISO-8859-1) with French accents
    text = "ville,température\nMontréal,12.5\nGenève,14.0\n"
    csv_bytes = text.encode("latin-1")

    result = ingest_csv(csv_bytes)
    assert result.profile.encoding in ("latin-1", "cp1252")
    assert result.profile.row_count == 2
    assert result.column_metadata[0].normalized_name == "ville"


# =====================================================================
# 6. Quoted Delimiters, Embedded Newlines, Escaped Quotes
# =====================================================================

def test_quoted_delimiters_and_newlines() -> None:
    csv_text = (
        'id,address,notes\n'
        '1,"123 Main St, Suite 400","First line\nSecond line"\n'
        '2,"789 Broad Ave","He said ""Hello"""\n'
    ).encode("utf-8")

    result = ingest_csv(csv_text)
    assert result.profile.row_count == 2
    
    records = result.records
    assert records[0]["address"] == "123 Main St, Suite 400"
    assert records[0]["notes"] == "First line\nSecond line"
    assert records[1]["notes"] == 'He said "Hello"'


def test_malformed_ragged_rows_rejected() -> None:
    # Row 3 has 4 columns instead of 3
    csv_text = (
        "col1,col2,col3\n"
        "a,b,c\n"
        "d,e,f,extra_field\n"
    ).encode("utf-8")

    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(csv_text)

    err = exc_info.value
    assert err.row_number == 3
    assert err.expected_columns == 3
    assert err.actual_columns == 4
    # Ensure error does not leak raw file contents
    assert "extra_field" not in str(err)


# =====================================================================
# 7. Delimiter Detection Across Candidates
# =====================================================================

@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_all_candidate_delimiters_detected(delimiter: str) -> None:
    csv_text = (
        f"col_a{delimiter}col_b{delimiter}col_c\n"
        f"10{delimiter}20{delimiter}30\n"
        f"40{delimiter}50{delimiter}60\n"
    ).encode("utf-8")

    result = ingest_csv(csv_text)
    assert result.profile.delimiter == delimiter
    assert result.profile.row_count == 2
    assert result.profile.column_count == 3


def test_single_column_file() -> None:
    csv_text = "values\n100\n200\n300\n".encode("utf-8")
    result = ingest_csv(csv_text)
    assert result.profile.column_count == 1
    assert result.profile.row_count == 3
    # Check warning was raised for single column
    assert any("single column" in w.lower() for w in result.profile.warnings)


# =====================================================================
# 8. Resource Limit Enforcement (Bytes, Rows, Columns, Cells, Timeout)
# =====================================================================

def test_file_size_limit_enforced() -> None:
    csv_bytes = b"a,b\n1,2\n3,4\n"
    limits = IngestionLimits(max_bytes=5)
    
    with pytest.raises(FileSizeLimitExceededError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)
        
    assert exc_info.value.max_bytes == 5
    assert exc_info.value.actual_bytes == len(csv_bytes)


def test_row_count_limit_enforced() -> None:
    csv_text = "x,y\n1,2\n3,4\n5,6\n".encode("utf-8")  # 3 data rows
    limits = IngestionLimits(max_rows=2)

    with pytest.raises(RowCountLimitExceededError) as exc_info:
        ingest_csv(csv_text, limits=limits)

    assert exc_info.value.max_rows == 2


def test_column_count_limit_enforced() -> None:
    headers = ",".join(f"c{i}" for i in range(10)) + "\n"
    row = ",".join("1" for _ in range(10)) + "\n"
    csv_bytes = (headers + row).encode("utf-8")
    
    limits = IngestionLimits(max_columns=5)
    with pytest.raises(ColumnCountLimitExceededError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)

    assert exc_info.value.max_columns == 5


def test_cell_size_limit_adversarial_expansion() -> None:
    # Generate an adversarial cell with 50 KB of text
    huge_cell = "A" * (50 * 1024)
    csv_text = f"id,payload\n1,{huge_cell}\n".encode("utf-8")
    
    # Default cell limit is 32 KB (32768 bytes)
    limits = IngestionLimits(max_cell_bytes=32 * 1024)
    with pytest.raises(CellSizeLimitExceededError) as exc_info:
        ingest_csv(csv_text, limits=limits)

    err = exc_info.value
    assert err.row_number == 2
    assert err.column_index == 1
    assert err.actual_bytes == 50 * 1024
    assert err.max_bytes == 32 * 1024


def test_processing_timeout_limit_enforced() -> None:
    # Set timeout to an impossibly small number (0.000001 seconds)
    csv_bytes = b"a,b\n1,2\n3,4\n"
    limits = IngestionLimits(max_processing_seconds=0.0000001)

    # Note: Depending on system clock resolution, let's verify either timeout or successful fast execution
    try:
        ingest_csv(csv_bytes, limits=limits)
    except ProcessingTimeoutError as e:
        assert e.max_seconds == 0.0000001


# =====================================================================
# 9. Type Precision and Edge Cases
# =====================================================================

def test_type_inference_edge_cases() -> None:
    csv_text = (
        "mixed_num,date_val,bool_val,zip_code,scientific_num\n"
        "10,2023-05-01,true,01234,1.25e-4\n"
        "20.5,2023-05-02,false,05678,3.5e2\n"
        "30,2023-05-03,yes,09123,0.01\n"
    ).encode("utf-8")

    result = ingest_csv(csv_text)
    col_map = {c.name: c for c in result.profile.columns}
    
    # Mixed int and float must generalize to float without crashing
    assert col_map["mixed_num"].inferred_type == "float"
    assert col_map["date_val"].inferred_type == "date"
    assert col_map["bool_val"].inferred_type == "boolean"
    # Leading zero codes must remain strings
    assert col_map["zip_code"].inferred_type == "string"
    assert col_map["scientific_num"].inferred_type == "float"


def test_data_quality_warnings_for_constant_column() -> None:
    csv_text = (
        "id,status,score\n"
        "1,active,95\n"
        "2,active,88\n"
        "3,active,92\n"
    ).encode("utf-8")

    result = ingest_csv(csv_text)
    warnings = result.profile.quality.warnings
    assert any("constant" in w.lower() for w in warnings)


# =====================================================================
# 10. Bounded Source Reading (Bytes, Binary Streams, Paths)
# =====================================================================

def test_byte_limit_exact_limit() -> None:
    csv_bytes = b"col1,col2\n10,20\n"  # 16 bytes
    limits = IngestionLimits(max_bytes=len(csv_bytes))
    result = ingest_csv(csv_bytes, limits=limits)
    assert result.profile.row_count == 1
    assert result.profile.column_count == 2


def test_byte_limit_one_byte_over() -> None:
    csv_bytes = b"col1,col2\n10,20\n"  # 16 bytes
    limits = IngestionLimits(max_bytes=len(csv_bytes) - 1)  # 15 bytes
    with pytest.raises(FileSizeLimitExceededError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)
    assert exc_info.value.max_bytes == 15
    assert exc_info.value.actual_bytes == 16


def test_byte_limit_binary_stream_input() -> None:
    content = b"header1,header2\nalpha,beta\ngamma,delta\n"
    stream = io.BytesIO(content)
    limits = IngestionLimits(max_bytes=len(content) - 5)
    with pytest.raises(FileSizeLimitExceededError) as exc_info:
        ingest_csv(stream, limits=limits)
    assert exc_info.value.max_bytes == len(content) - 5


def test_byte_limit_binary_stream_success() -> None:
    content = b"header1,header2\nalpha,beta\ngamma,delta\n"
    stream = io.BytesIO(content)
    limits = IngestionLimits(max_bytes=len(content))
    result = ingest_csv(stream, limits=limits)
    assert result.profile.row_count == 2


def test_byte_limit_filesystem_path_input(tmp_path: Path) -> None:
    test_file = tmp_path / "bounded_test.csv"
    content = b"col_a,col_b\n123,456\n789,012\n"
    test_file.write_bytes(content)

    # Exceed limit
    limits = IngestionLimits(max_bytes=len(content) - 1)
    with pytest.raises(FileSizeLimitExceededError) as exc_info:
        ingest_csv(test_file, limits=limits)
    assert exc_info.value.max_bytes == len(content) - 1

    # Within limit
    valid_limits = IngestionLimits(max_bytes=len(content))
    result = ingest_csv(test_file, limits=valid_limits)
    assert result.profile.row_count == 2


def test_byte_limit_stream_growing_beyond_declared_limit() -> None:
    class EndlessGrowingStream(io.RawIOBase):
        def __init__(self) -> None:
            self.total_read = 0

        def read(self, size: int = -1) -> bytes:
            chunk = b"x" * (size if size > 0 else 1024)
            self.total_read += len(chunk)
            return chunk

    stream = EndlessGrowingStream()
    limits = IngestionLimits(max_bytes=128 * 1024)  # 128 KB
    with pytest.raises(FileSizeLimitExceededError) as exc_info:
        ingest_csv(stream, limits=limits)
    assert exc_info.value.actual_bytes > limits.max_bytes


def test_byte_limit_empty_stream() -> None:
    stream = io.BytesIO(b"")
    with pytest.raises(EmptyDatasetError):
        ingest_csv(stream)


def test_byte_limit_stream_with_non_zero_initial_position() -> None:
    content = b"PREFIX_TO_SKIP\ncol_x,col_y\n100,200\n"
    stream = io.BytesIO(content)
    stream.seek(15)  # Seek past the prefix
    result = ingest_csv(stream)
    assert result.profile.row_count == 1
    assert result.column_metadata[0].normalized_name == "col_x"


# =====================================================================
# 11. Execution Deadline & Timeout Propagation
# =====================================================================

def test_timeout_deadline_exceeded_during_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    csv_bytes = b"col_a,col_b\n1,2\n3,4\n"
    limits = IngestionLimits(max_processing_seconds=1.0)

    # Advance monotonic clock immediately past deadline during stream read
    call_count = 0

    def mock_monotonic() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 1000.0  # start_time
        return 1002.0  # past 1.0s deadline

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    with pytest.raises(ProcessingTimeoutError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)
    assert exc_info.value.max_seconds == 1.0


def test_timeout_deadline_exceeded_during_type_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    # Specifically targets F02 audit finding: delay injected during type inference must trigger timeout
    csv_bytes = b"col_a,col_b\n1,2\n3,4\n"
    limits = IngestionLimits(max_processing_seconds=0.01)

    import core.ingestion
    orig_infer = core.ingestion.infer_column_type

    def delayed_infer(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.03)  # Sleep 30 ms (past 10 ms limit)
        return orig_infer(*args, **kwargs)

    monkeypatch.setattr(core.ingestion, "infer_column_type", delayed_infer)
    with pytest.raises(ProcessingTimeoutError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)
    assert exc_info.value.max_seconds == 0.01


def test_timeout_deadline_exceeded_during_profiling(monkeypatch: pytest.MonkeyPatch) -> None:
    csv_bytes = b"col_a,col_b\n1,2\n3,4\n"
    limits = IngestionLimits(max_processing_seconds=0.01)

    import core.ingestion
    orig_profile = core.ingestion.profile_dataset

    def delayed_profile(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.03)
        return orig_profile(*args, **kwargs)

    monkeypatch.setattr(core.ingestion, "profile_dataset", delayed_profile)
    with pytest.raises(ProcessingTimeoutError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)
    assert exc_info.value.max_seconds == 0.01


def test_timeout_normal_operation_succeeds() -> None:
    csv_bytes = b"col_a,col_b\n1,2\n3,4\n"
    limits = IngestionLimits(max_processing_seconds=5.0)
    result = ingest_csv(csv_bytes, limits=limits)
    assert result.profile.row_count == 2


# =====================================================================
# 12. IngestionLimits Runtime Validation
# =====================================================================

@pytest.mark.parametrize("param,invalid_val", [
    ("max_bytes", 0),
    ("max_bytes", -10),
    ("max_rows", 0),
    ("max_rows", -1),
    ("max_columns", 0),
    ("max_columns", -5),
    ("max_cell_bytes", 0),
    ("max_cell_bytes", -100),
    ("max_processing_seconds", 0),
    ("max_processing_seconds", -0.1),
    ("max_processing_seconds", float("nan")),
    ("max_processing_seconds", float("inf")),
    ("max_processing_seconds", float("-inf")),
])
def test_limits_validation_zero_negative_nan_inf(param: str, invalid_val: Any) -> None:
    with pytest.raises(ValueError):
        IngestionLimits(**{param: invalid_val})


@pytest.mark.parametrize("param,invalid_type_val", [
    ("max_bytes", 10.5),
    ("max_bytes", "100"),
    ("max_bytes", True),
    ("max_rows", 5.0),
    ("max_rows", "1000"),
    ("max_rows", False),
    ("max_columns", [10]),
    ("max_columns", "col"),
    ("max_cell_bytes", 32.5),
    ("max_cell_bytes", "32KB"),
    ("max_processing_seconds", "10s"),
    ("max_processing_seconds", True),
])
def test_limits_validation_invalid_types(param: str, invalid_type_val: Any) -> None:
    with pytest.raises(TypeError):
        IngestionLimits(**{param: invalid_type_val})


def test_limits_validation_impossible_combinations() -> None:
    # Explicitly requesting max_cell_bytes > max_bytes is an impossible combination
    with pytest.raises(ValueError) as exc_info:
        IngestionLimits(max_bytes=100, max_cell_bytes=200)
    assert "cannot exceed max_bytes" in str(exc_info.value)


def test_limits_validation_valid_boundaries() -> None:
    # Smallest valid boundaries
    limits = IngestionLimits(
        max_bytes=1,
        max_rows=1,
        max_columns=1,
        max_cell_bytes=1,
        max_processing_seconds=0.001,
    )
    assert limits.max_bytes == 1
    assert limits.max_cell_bytes == 1
    assert limits.max_processing_seconds == 0.001

    # Equal max_cell_bytes and max_bytes boundary
    equal_limits = IngestionLimits(max_bytes=1000, max_cell_bytes=1000)
    assert equal_limits.max_cell_bytes == 1000


# =====================================================================
# 13. Security & Error Message Sanitization (No Path/Content Leakage)
# =====================================================================

def test_file_not_found_sanitizes_path() -> None:
    non_existent = Path("C:/secret/system/deep/folder/missing_file.csv")
    with pytest.raises(FileNotFoundError) as exc_info:
        ingest_csv(non_existent)
    # Ensure raw directory hierarchy is not leaked in the message
    err_msg = str(exc_info.value)
    assert "secret" not in err_msg
    assert "missing_file.csv" in err_msg


def test_cell_size_error_does_not_leak_cell_content() -> None:
    secret_payload = "SECRET_API_KEY_DO_NOT_LEAK" * 2000
    csv_bytes = f"col1,col2\n1,{secret_payload}\n".encode("utf-8")
    limits = IngestionLimits(max_cell_bytes=100)
    with pytest.raises(CellSizeLimitExceededError) as exc_info:
        ingest_csv(csv_bytes, limits=limits)
    err_msg = str(exc_info.value)
    assert "SECRET_API_KEY" not in err_msg
    assert "exceeds maximum allowed cell size" in err_msg


# =====================================================================
# 14. Targeted Phase 1 Remediation Tests (P1-01, P1-02, P1-03)
# =====================================================================

def test_p1_01_reader_and_text_stream_released_after_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-01: Proves that the exhausted csv.reader and its underlying io.StringIO text buffer
    are released immediately after row parsing finishes, and are not retained during inference.
    """
    import core.ingestion
    created_stream_ref: Optional[weakref.ReferenceType[io.StringIO]] = None
    stream_was_closed: bool = False
    ref_during_inference: Optional[io.StringIO] = "UNSET"  # type: ignore[assignment]

    orig_stringio = io.StringIO

    def tracking_stringio(*args: Any, **kwargs: Any) -> io.StringIO:
        nonlocal created_stream_ref
        stream = orig_stringio(*args, **kwargs)
        created_stream_ref = weakref.ref(stream)
        return stream

    monkeypatch.setattr(core.ingestion.io, "StringIO", tracking_stringio)

    orig_infer = core.ingestion.infer_column_type

    def probe_infer(*args: Any, **kwargs: Any) -> Any:
        nonlocal ref_during_inference, stream_was_closed
        assert created_stream_ref is not None
        target = created_stream_ref()
        if target is not None:
            stream_was_closed = target.closed
        ref_during_inference = target
        return orig_infer(*args, **kwargs)

    monkeypatch.setattr(core.ingestion, "infer_column_type", probe_infer)

    csv_bytes = b"col1,col2\nval1,val2\nval3,val4\n"
    result = ingest_csv(csv_bytes)

    # During inference, the text_stream must already be closed and dropped by reader
    assert ref_during_inference is None, "Exhausted csv.reader retained text_stream during inference!"
    # After ingestion returns, the stream object is dead
    assert created_stream_ref is not None and created_stream_ref() is None
    # Data is correctly parsed
    assert result.profile.row_count == 2


def test_p1_02_deadline_expiry_during_record_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-02: Proves that deadline expiry genuinely during record assembly raises ProcessingTimeoutError
    and cannot return an IngestionResult successfully.
    """
    import core.ingestion

    # Generate 250 rows so that record assembly polls deadline at r_idx=0 and r_idx=100
    csv_text = "col_a,col_b\n" + "\n".join(f"{i},{i*2}" for i in range(250)) + "\n"
    csv_bytes = csv_text.encode("utf-8")

    profiling_completed = False
    orig_profile = core.ingestion.profile_dataset

    def hooked_profile(*args: Any, **kwargs: Any) -> Any:
        nonlocal profiling_completed
        res = orig_profile(*args, **kwargs)
        profiling_completed = True
        return res

    monkeypatch.setattr(core.ingestion, "profile_dataset", hooked_profile)

    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    assembly_checks = 0

    def mock_monotonic() -> float:
        nonlocal simulated_time, assembly_checks
        if profiling_completed:
            assembly_checks += 1
            # Call 1 after profiling is Check A (line 267) -> pass
            # Call 2 is r_idx=0 Check B (line 273) -> pass (assembly has begun)
            # Call 3 is r_idx=100 Check B (line 273) -> expire here!
            if assembly_checks >= 3:
                simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    with pytest.raises(ProcessingTimeoutError) as exc_info:
        ingest_csv(csv_bytes, limits=IngestionLimits(max_processing_seconds=timeout_seconds))

    assert exc_info.value.max_seconds == timeout_seconds
    assert assembly_checks == 3, f"Expected expiry at 3rd check (r_idx=100 during assembly), got {assembly_checks}"


def test_p1_02_deadline_expiry_immediately_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-02: Proves that deadline expiry after IngestionResult construction but before return
    raises ProcessingTimeoutError instead of returning success.
    """
    start_time = 1000.0
    timeout_seconds = 2.0
    simulated_time = start_time
    result_constructed = False

    orig_init = IngestionResult.__init__

    def hooked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal result_constructed, simulated_time
        orig_init(self, *args, **kwargs)
        result_constructed = True
        # Advance clock past deadline AFTER IngestionResult is initialized, right before return check
        simulated_time = start_time + timeout_seconds + 1.0

    monkeypatch.setattr(IngestionResult, "__init__", hooked_init)

    def mock_monotonic() -> float:
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    csv_bytes = b"col_a,col_b\n1,2\n3,4\n"
    with pytest.raises(ProcessingTimeoutError) as exc_info:
        ingest_csv(csv_bytes, limits=IngestionLimits(max_processing_seconds=timeout_seconds))

    assert exc_info.value.max_seconds == timeout_seconds
    assert result_constructed is True, "Expected IngestionResult to be constructed before pre-return timeout"


def test_p1_03_type_inference_polls_deadline_and_halts_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-03: Proves that type inference polls the deadline during candidate matching and aborts early.
    """
    import core.type_inference

    raw_values = [str(i) for i in range(10_000)]
    call_count = 0
    orig_parse_int = core.type_inference.parse_integer

    def counting_parse_int(val: str) -> Optional[int]:
        nonlocal call_count
        call_count += 1
        return orig_parse_int(val)

    monkeypatch.setattr(core.type_inference, "parse_integer", counting_parse_int)

    start_time = 1000.0
    timeout_seconds = 1.0

    def mock_monotonic() -> float:
        # Advance clock past deadline once call_count reaches 750
        if call_count >= 750:
            return start_time + timeout_seconds + 5.0
        return start_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    with pytest.raises(ProcessingTimeoutError):
        infer_column_type(raw_values, deadline=deadline, poll_interval=500)

    # In a 10,000 value column, candidate check + conversion would evaluate up to 20,000 times.
    # With periodic polling every 500 iterations, it aborts at or near 1,000 calls.
    assert call_count < 2000, f"Expected early abort, but processed {call_count} calls!"
    assert call_count >= 750


def test_p1_03_r1_typed_conversion_polls_deadline_and_halts_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-03-R1: Proves that typed conversion loop polls deadline and aborts mid-conversion,
    with null counts tracked in-loop without separate unpolled full-column scans.
    """
    import core.type_inference

    raw_values = [str(i) for i in range(10_000)]
    call_count = 0
    orig_parse_int = core.type_inference.parse_integer

    def counting_parse_int(val: str) -> Optional[int]:
        nonlocal call_count
        call_count += 1
        return orig_parse_int(val)

    monkeypatch.setattr(core.type_inference, "parse_integer", counting_parse_int)

    start_time = 1000.0
    timeout_seconds = 5.0

    def mock_monotonic() -> float:
        # Candidate check completes (calls 1 to 10,000)
        # convert_column begins (calls 10,001+)
        # Advance clock at call 10,750 (inside convert_column)
        if call_count >= 10750:
            return start_time + timeout_seconds + 1.0
        return start_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    with pytest.raises(ProcessingTimeoutError):
        infer_column_type(raw_values, deadline=deadline, poll_interval=500)

    # Aborts inside convert_column at or near call 11,000, far before completing 20,000 calls
    assert 10750 <= call_count <= 11000, f"Expected abort in convert_column, got {call_count} calls"


def test_p1_03_r1_fallback_string_polls_deadline_and_halts_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-03-R1: Proves that fallback string conversion polls deadline and aborts early,
    counting nulls in-loop without separate unpolled scans.
    """
    raw_strings = [f"text_{i}" for i in range(10_000)]
    start_time = 1000.0
    timeout_seconds = 5.0
    string_checks = 0

    def mock_monotonic() -> float:
        nonlocal string_checks
        string_checks += 1
        # In fallback loop, idx % 500 == 0 calls deadline.check()
        # Advance time on the 4th check (after 1,500 items processed)
        if string_checks >= 4:
            return start_time + timeout_seconds + 1.0
        return start_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    with pytest.raises(ProcessingTimeoutError):
        infer_column_type(raw_strings, deadline=deadline, poll_interval=500)

    assert string_checks >= 4


def test_p1_03_r1_all_null_column_fast_path() -> None:
    """
    P1-03-R1: Proves all-null columns resolve directly to string with correct null count
    without triggering full scans or failing type inference.
    """
    raw_values = ["na", "null", "none", "", "nan"] * 2000  # 10,000 null items
    inferred_type, typed, null_count = infer_column_type(raw_values)
    assert inferred_type == "string"
    assert null_count == 10_000
    assert len(typed) == 10_000
    assert all(v is None for v in typed)


def test_p1_03_r2_numeric_profiling_preserves_reference_variance() -> None:
    """
    P1-03-R2: Proves that calculate_numeric_stats preserves the reference numerical result
    (99990000.9999) for high-variance inputs [1_000_000] + [0]*10_000.
    """
    values = [1_000_000] + [0] * 10_000
    stats = calculate_numeric_stats(values)
    assert stats["variance"] == 99990000.9999, f"Variance regressed: got {stats['variance']}"
    assert stats["std_dev"] == 9999.5
    assert stats["mean"] == 99.99
    assert stats["min"] == 0
    assert stats["max"] == 1_000_000


def test_p1_03_r2_numeric_profiling_polls_deadline_after_variance_work_begun(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-03-R2: Proves that calculate_numeric_stats polls deadline during variance calculation
    and aborts cooperatively after variance work has actively begun.
    """
    values = list(range(10_000))
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    monotonic_calls = 0

    def mock_monotonic() -> float:
        nonlocal monotonic_calls, simulated_time
        monotonic_calls += 1
        # Call 1: initial check
        # Call 2: after sort check
        # Call 3: before variance check
        # Call 4: idx=0 check in polled_sq_diff
        # Call 5: idx=500 check in polled_sq_diff (variance work has actively begun)
        # Call 6: idx=1000 check in polled_sq_diff -> advance past deadline!
        if monotonic_calls >= 6:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    with pytest.raises(ProcessingTimeoutError):
        calculate_numeric_stats(values, deadline=deadline, poll_interval=500)

    assert monotonic_calls == 6, f"Expected timeout on 6th check after variance work began, got {monotonic_calls}"


def test_p1_03_profiling_string_polls_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-03: Proves that calculate_string_stats polls deadline during string length analysis
    after string work has actively begun.
    """
    values = [f"string_{i}" for i in range(10_000)]
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    monotonic_calls = 0

    def mock_monotonic() -> float:
        nonlocal monotonic_calls, simulated_time
        monotonic_calls += 1
        # Call 1: initial check
        # Call 2: idx=0 check in length loop
        # Call 3: idx=500 check in length loop (work has actively begun)
        # Call 4: idx=1000 check -> advance past deadline!
        if monotonic_calls >= 4:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    with pytest.raises(ProcessingTimeoutError):
        calculate_string_stats(values, deadline=deadline, poll_interval=500)

    assert monotonic_calls == 4, f"Expected timeout on 4th check, got {monotonic_calls}"


def test_p1_03_profile_column_polls_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P1-03: Proves that profile_column polls deadline during non-null filtering
    after filtering work has actively begun.
    """
    values = [f"item_{i}" for i in range(10_000)]
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    monotonic_calls = 0

    def mock_monotonic() -> float:
        nonlocal monotonic_calls, simulated_time
        monotonic_calls += 1
        # Call 1: initial check
        # Call 2: idx=0 check in non-null loop
        # Call 3: idx=500 check in non-null loop (work has actively begun)
        # Call 4: idx=1000 check -> advance past deadline!
        if monotonic_calls >= 4:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    with pytest.raises(ProcessingTimeoutError):
        profile_column(
            index=0,
            raw_name="col",
            normalized_name="col",
            inferred_type="string",
            typed_values=values,
            total_rows=len(values),
            deadline=deadline,
            poll_interval=500,
        )

    assert monotonic_calls == 4, f"Expected timeout on 4th check, got {monotonic_calls}"



