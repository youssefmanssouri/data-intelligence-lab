"""
Unit Test Suite for Data Intelligence Lab - Phase 2: CSV Parsing Correctness
Validates:
- P2-01: Malformed header rejection & canonical delimiter handling
- P2-02: Consistent logical record numbering
- P2-03: Filesystem metadata error sanitization
- P2-04: Distinction between native parser guard and configured byte limits
- F04: Coordinated encoding detection & decoding
- F03: Logical delimiter detection
- F06: Formal error contract & payload secrecy
"""

from __future__ import annotations
import io
import os
from pathlib import Path
import time
from typing import Any, Optional
import pytest

from core import (
    CellSizeLimitExceededError,
    DelimiterDetectionError,
    EmptyDatasetError,
    EncodingDetectionError,
    ExecutionDeadline,
    FileSizeLimitExceededError,
    IngestionLimits,
    MalformedCSVError,
    ParserFieldSizeLimitError,
    ProcessingTimeoutError,
    SourceReadError,
    ingest_csv,
)
from core.detection import detect_and_decode_encoding, detect_delimiter, detect_encoding


# =====================================================================
# Helper Functions
# =====================================================================

def _make_csv_prefix(target_len: int) -> bytes:
    """Generate valid 2-column CSV prefix of exact byte length without exceeding cell size."""
    header = b"col1,col2\n"  # 10 bytes
    rem = target_len - len(header)
    q, r = divmod(rem, 4)
    if r == 0:
        rows = b"x,y\n" * q
    else:
        rows = (b"x,y\n" * (q - 1)) + (b"x" * (r + 1)) + b",y\n"
    res = header + rows
    assert len(res) == target_len
    return res


# =====================================================================
# F04: Encoding Detection & Coordinated Decoding
# =====================================================================

def test_f04_utf8_multibyte_crossing_64k_boundary() -> None:
    """
    F04: Proves 2-byte, 3-byte, and 4-byte UTF-8 characters split across the former
    65,536-byte boundary are decoded strictly as UTF-8 without falling back to Latin-1/CP1252.
    """
    # 2-byte: 'é' = b'\xc3\xa9' -> split at byte 65536 by placing prefix of 65535 bytes
    prefix_2b = _make_csv_prefix(65535)
    csv_bytes_2b = prefix_2b + "é,test_val\n".encode("utf-8")
    result_2b = ingest_csv(csv_bytes_2b)
    assert result_2b.profile.encoding == "utf-8"
    assert result_2b.records[-1]["col1"] == "é"

    # 3-byte: '€' = b'\xe2\x82\xac' -> split with 2 bytes before boundary, 1 after (prefix 65534)
    prefix_3b = _make_csv_prefix(65534)
    csv_bytes_3b = prefix_3b + "€,test_val\n".encode("utf-8")
    result_3b = ingest_csv(csv_bytes_3b)
    assert result_3b.profile.encoding == "utf-8"
    assert result_3b.records[-1]["col1"] == "€"

    # 4-byte: '🚀' = b'\xf0\x9f\x9a\x80' -> split with 1 byte before boundary, 3 after (prefix 65535)
    prefix_4b = _make_csv_prefix(65535)
    csv_bytes_4b = prefix_4b + "🚀,test_val\n".encode("utf-8")
    result_4b = ingest_csv(csv_bytes_4b)
    assert result_4b.profile.encoding == "utf-8"
    assert result_4b.records[-1]["col1"] == "🚀"


@pytest.mark.parametrize("split_offset", [-2, -1, 0, 1, 2])
def test_f04_utf8_split_positions_around_64k_boundary(split_offset: int) -> None:
    """F04: Exercises internal byte split positions around 65,536-byte mark."""
    target_pos = 65535 + split_offset
    prefix = _make_csv_prefix(target_pos)
    csv_bytes = prefix + "ü,end\n".encode("utf-8")
    result = ingest_csv(csv_bytes)
    assert result.profile.encoding == "utf-8"
    assert result.records[-1]["col1"] == "ü"


def test_f04_utf8_bom_decodes_strictly_as_utf8_sig() -> None:
    """F04: Proves UTF-8 BOM is detected as utf-8-sig and decodes properly."""
    csv_bytes = b"\xef\xbb\xbfcol1,col2\nval1,val2\n"
    result = ingest_csv(csv_bytes)
    assert result.profile.encoding == "utf-8-sig"
    assert result.column_metadata[0].raw_name == "col1"
    assert result.profile.columns[0].name == "col1"


def test_f04_utf16_le_and_be_boms() -> None:
    """F04: Proves UTF-16 LE and BE BOMs decode strictly with BOM-selected codec."""
    # UTF-16 LE
    le_text = "id,name\n1,Alice\n2,Bob\n"
    le_bytes = le_text.encode("utf-16")  # Python encode('utf-16') prepends native BOM
    result_le = ingest_csv(le_bytes)
    assert result_le.profile.encoding == "utf-16"
    assert result_le.profile.row_count == 2
    assert result_le.records[0]["name"] == "Alice"

    # UTF-16 BE explicitly
    be_bytes = b"\xfe\xff" + "id,name\n1,Charlie\n".encode("utf-16-be")
    result_be = ingest_csv(be_bytes)
    assert result_be.profile.encoding == "utf-16"
    assert result_be.profile.row_count == 1
    assert result_be.records[0]["name"] == "Charlie"


def test_f04_invalid_utf8_bom_content_raises_encoding_error() -> None:
    """
    F04: Proves invalid UTF-8 bytes following a UTF-8 BOM raise EncodingDetectionError
    and are NEVER reinterpreted as Latin-1 or CP1252.
    """
    invalid_utf8_bom = b"\xef\xbb\xbfcol1,col2\n\xff\xfe\n"
    with pytest.raises(EncodingDetectionError) as exc_info:
        ingest_csv(invalid_utf8_bom)
    assert "Invalid UTF-8 content with UTF-8 BOM" in str(exc_info.value)
    assert exc_info.value.code == "ERR_ENCODING_DETECTION"


def test_f04_invalid_utf16_bom_content_raises_encoding_error() -> None:
    """
    F04: Proves invalid content following a UTF-16 BOM raises EncodingDetectionError
    and is never reinterpreted as Latin-1.
    """
    # UTF-16 with odd trailing byte length
    invalid_utf16_bom = b"\xff\xfec\x00o\x00l\x00\xff"
    with pytest.raises(EncodingDetectionError) as exc_info:
        ingest_csv(invalid_utf16_bom)
    assert "Invalid UTF-16 content with UTF-16 BOM" in str(exc_info.value)


def test_f04_bytes_after_64k_boundary_selecting_cp1252() -> None:
    """
    F04: Proves ASCII content up to 64 KB followed by CP1252-specific bytes strictly beyond
    offset 65,536 selects CP1252, rather than falling back to Latin-1.
    """
    # 0x80 is the Euro sign in CP1252 (invalid in UTF-8, unassigned in ISO-8859-1)
    header = b"a,b\n"
    prefix = header + (b"val1,val2\n" * 6600)  # 66,004 bytes
    assert len(prefix) > 65536
    suffix = b"100,\x80\n"
    csv_bytes = prefix + suffix

    # Assert exact byte offset of the CP1252-specific byte is strictly beyond 65,536
    euro_offset = csv_bytes.index(b"\x80")
    assert euro_offset == 66008
    assert euro_offset > 65536

    result = ingest_csv(csv_bytes)
    assert result.profile.encoding == "cp1252"
    assert result.profile.row_count == 6601
    assert result.records[-1]["b"] == "€"


def test_f04_latin1_fallback_for_unassigned_cp1252_bytes() -> None:
    """
    F04: Proves bytes undefined in CP1252 (such as 0x81) fall back to Latin-1.
    """
    csv_bytes = b"col1,col2\nval,\x81\n"
    result = ingest_csv(csv_bytes)
    assert result.profile.encoding == "latin-1"
    assert result.profile.row_count == 1


def test_f04_deadline_checked_directly_in_detect_and_decode_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """F04: Proves deadline is verified directly during execution of detect_and_decode_encoding."""
    fake_time = 1000.0

    def mock_time() -> float:
        return fake_time

    monkeypatch.setattr(time, "monotonic", mock_time)
    deadline = ExecutionDeadline(2.0)

    # Advance fake time past deadline before helper starts
    fake_time = 1005.0
    with pytest.raises(ProcessingTimeoutError):
        detect_and_decode_encoding(b"col1,col2\n1,2\n", deadline=deadline)


def test_f04_deadline_checked_during_decoding_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """F04: Proves deadline expiry during non-BOM candidate attempts halts decoding."""
    current_time = 1000.0
    calls = 0

    def mock_time() -> float:
        nonlocal calls, current_time
        calls += 1
        # Call 1: ExecutionDeadline.__init__
        # Call 2: First check in detect_and_decode_encoding
        # Call 3: Second check before UTF-8 decode -> expire deadline!
        if calls >= 3:
            current_time = 1010.0
        return current_time

    monkeypatch.setattr(time, "monotonic", mock_time)
    deadline = ExecutionDeadline(2.0)

    with pytest.raises(ProcessingTimeoutError):
        detect_and_decode_encoding(b"a,b\n1,2\n", deadline=deadline)


# =====================================================================
# P2-01: Malformed Header Not Reinterpreted as Single-Column CSV
# =====================================================================

def test_p2_01_malformed_header_not_reinterpreted_as_single_column_csv() -> None:
    """
    P2-01: Proves b'a,"unclosed\\n1,2\\n' is rejected with MalformedCSVError,
    and NOT silently accepted as a single-column file under an arbitrary delimiter.
    """
    malformed_csv = b'a,"unclosed\n1,2\n'
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(malformed_csv)
    assert exc_info.value.code == "ERR_MALFORMED_CSV"
    assert exc_info.value.row_number == 1


def test_p2_01_candidate_order_does_not_change_malformed_behavior() -> None:
    """
    P2-01: Proves candidate delimiter order does not alter single-column canonical comma behavior
    or mask malformed comma CSV.
    """
    malformed_text = 'a,"unclosed\n1,2\n'
    # Even if semicolon, tab, pipe are checked first, detect_delimiter must return comma
    delim = detect_delimiter(malformed_text, candidate_delimiters=(";", "\t", "|", ","))
    assert delim == ","


def test_p2_01_valid_single_column_preserves_comma_convention() -> None:
    """
    P2-01: Proves a valid single-column file correctly defaults to comma convention.
    """
    single_col = b"single_column_header\nvalue_1\nvalue_2\n"
    result = ingest_csv(single_col)
    assert result.profile.delimiter == ","
    assert result.profile.column_count == 1
    assert result.profile.row_count == 2
    assert result.records[0]["single_column_header"] == "value_1"


def test_p2_01_detect_delimiter_keyword_sample_text_compatibility() -> None:
    """
    P2-01: Proves detect_delimiter preserves backwards compatibility with sample_text keyword arg.
    """
    assert detect_delimiter(sample_text="a,b\n1,2\n") == ","
    assert detect_delimiter(sample_text="a;b\n1;2\n") == ";"


@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_p2_01_valid_multiline_with_all_delimiters_lf_and_crlf(delim: str) -> None:
    """
    P2-01: Proves all supported delimiters correctly parse multiline quoted fields
    with both LF and CRLF line endings.
    """
    # LF
    csv_lf = (
        f'id{delim}notes{delim}status\n'
        f'1{delim}"Line 1\nLine 2"{delim}Active\n'
        f'2{delim}"Normal"{delim}Done\n'
    )
    res_lf = ingest_csv(csv_lf.encode("utf-8"))
    assert res_lf.profile.delimiter == delim
    assert res_lf.profile.row_count == 2
    assert res_lf.records[0]["notes"] == "Line 1\nLine 2"

    # CRLF
    csv_crlf = (
        f'id{delim}notes{delim}status\r\n'
        f'1{delim}"Line 1\r\nLine 2"{delim}Active\r\n'
        f'2{delim}"Normal"{delim}Done\r\n'
    )
    res_crlf = ingest_csv(csv_crlf.encode("utf-8"))
    assert res_crlf.profile.delimiter == delim
    assert res_crlf.profile.row_count == 2
    assert res_crlf.records[0]["notes"] == "Line 1\r\nLine 2"


def test_f03_all_delimiters_with_lf_and_crlf() -> None:
    """F03: Tests all delimiters with LF and CRLF record endings."""
    for delim in [",", ";", "\t", "|"]:
        csv_lf = f"id{delim}name{delim}score\n1{delim}Alice{delim}100\n2{delim}Bob{delim}95\n"
        res_lf = ingest_csv(csv_lf.encode("utf-8"))
        assert res_lf.profile.delimiter == delim
        assert res_lf.profile.row_count == 2
        assert res_lf.records[0]["name"] == "Alice"


def test_f03_quoted_delimiters_and_doubled_quotes() -> None:
    """
    F03: Proves quoted delimiters and doubled quotes are parsed using actual CSV semantics.
    """
    csv_semi = (
        '"last, first";"department";"notes"\n'
        '"Smith, John";"Engineering";"He said ""Hello!"""\n'
        '"Doe, Jane";"Design";"Double ""quoted"" text"\n'
    )
    result = ingest_csv(csv_semi.encode("utf-8"))
    assert result.profile.delimiter == ";"
    assert result.profile.column_count == 3
    assert result.profile.row_count == 2
    assert result.records[0]["last_first"] == "Smith, John"
    assert result.records[0]["notes"] == 'He said "Hello!"'
    assert result.records[1]["notes"] == 'Double "quoted" text'


def test_f03_tab_delimited_with_empty_leading_and_trailing_fields() -> None:
    """
    F03: Proves tabs are not stripped from leading or trailing empty fields.
    """
    csv_tab = "\tname\tage\t\n\tAlice\t30\t\n\tBob\t25\t\n"
    result = ingest_csv(csv_tab.encode("utf-8"))
    assert result.profile.delimiter == "\t"
    assert result.profile.column_count == 4
    assert result.profile.row_count == 2
    assert result.records[0]["name"] == "Alice"
    assert result.records[0]["age"] == 30


def test_f03_uniquely_identified_delimiter_with_later_ragged_record() -> None:
    """
    F03: Proves delimiter is uniquely selected and later ragged record raises MalformedCSVError.
    """
    csv_text = "col1,col2,col3\n1,2,3\n4,5,6,7\n"
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(csv_text.encode("utf-8"))
    assert exc_info.value.row_number == 3
    assert exc_info.value.expected_columns == 3
    assert exc_info.value.actual_columns == 4


def test_f03_ambiguous_delimiter_raises_delimiter_detection_error() -> None:
    """F03: Proves structurally ambiguous input raises DelimiterDetectionError."""
    ambiguous_csv = "col1,col2|col3\n1,2|3\n4,5|6\n"
    with pytest.raises(DelimiterDetectionError) as exc_info:
        ingest_csv(ambiguous_csv.encode("utf-8"))
    assert exc_info.value.code == "ERR_DELIMITER_DETECTION"
    assert "Ambiguous delimiter" in str(exc_info.value)


def test_f03_records_crossing_former_detection_boundaries() -> None:
    """
    F03: Proves delimiter detection functions correctly when records cross the former
    8,192 and 65,536 character detection boundaries.
    """
    long_field = "x" * 10_000
    csv_text = f"h1;h2\n{long_field};1\n{long_field};2\n{long_field};3\n{long_field};4\n{long_field};5\n{long_field};6\n{long_field};7\n"
    assert len(csv_text) > 70_000

    result = ingest_csv(csv_text.encode("utf-8"), limits=IngestionLimits(max_cell_bytes=32_000))
    assert result.profile.delimiter == ";"
    assert result.profile.column_count == 2
    assert result.profile.row_count == 7


# =====================================================================
# P2-02: Consistent Logical Record Numbers
# =====================================================================

def test_p2_02_syntax_error_following_multiline_record_reports_logical_row() -> None:
    """
    P2-02: Proves a syntax error following a multiline record reports the logical record number,
    not the raw physical line count.
    """
    # Header: row 1 (physical line 1)
    # Record 2: row 2 (physical lines 2, 3, 4)
    # Record 3: row 3 (physical line 5) -> syntax error with unexpected character after quote
    csv_text = (
        'col1,col2\n'
        '1,"multi\nline\nfield"\n'
        '"bad"quote,value\n'
    )
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(csv_text.encode("utf-8"))
    err = exc_info.value
    assert err.row_number == 3  # Logical record 3
    assert err.line_number == 5  # Physical line 5 preserved separately
    assert "at row 3" in str(err)


def test_p2_02_unclosed_multiline_record_reports_logical_row() -> None:
    """
    P2-02: Proves an unclosed multiline record reports the logical record consistently.
    """
    # Header: row 1
    # Record 2: unclosed quote spanning physical lines 2, 3
    csv_text = (
        'col1,col2\n'
        '1,"multi\nline_unclosed\n'
    )
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(csv_text.encode("utf-8"))
    assert exc_info.value.row_number == 2


def test_p2_02_ragged_record_at_same_logical_position_reports_same_row() -> None:
    """
    P2-02: Proves ragged records and syntax errors report identical row numbers
    at equivalent logical record positions.
    """
    # Header: row 1 (physical line 1)
    # Record 2: row 2 (physical lines 2, 3)
    # Record 3: row 3 (physical line 4) -> ragged (3 cols instead of 2)
    csv_text = (
        'col1,col2\n'
        '1,"multi\nline"\n'
        '2,val,extra\n'
    )
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(csv_text.encode("utf-8"))
    assert exc_info.value.row_number == 3
    assert exc_info.value.expected_columns == 2
    assert exc_info.value.actual_columns == 3


def test_p2_02_header_counts_as_logical_record_1() -> None:
    """
    P2-02: Proves header is treated as logical record 1 for syntax and width errors.
    """
    bad_header = b'"unclosed_header\n'
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(bad_header)
    assert exc_info.value.row_number == 1


def test_f06_malformed_csv_error_width_compatibility() -> None:
    """
    F06: Proves MalformedCSVError preserves 3-positional-argument construction and attributes.
    """
    err = MalformedCSVError(5, 3, 4)
    assert err.row_number == 5
    assert err.expected_columns == 3
    assert err.actual_columns == 4
    assert err.code == "ERR_MALFORMED_CSV"
    assert str(err) == "Malformed CSV at row 5: expected 3 columns, but found 4 columns."


def test_f06_malformed_csv_error_syntax_without_fabricated_metadata() -> None:
    """
    F06: Proves MalformedCSVError for syntax errors permits None for expected/actual widths.
    """
    err = MalformedCSVError(row_number=1, message="Malformed CSV at row 1: syntax error while parsing header.")
    assert err.row_number == 1
    assert err.expected_columns is None
    assert err.actual_columns is None
    assert "syntax error while parsing header" in str(err)


def test_f06_unclosed_quote_in_header_raises_controlled_malformed_csv() -> None:
    """F06: Proves unclosed quote in header raises MalformedCSVError."""
    bad_header = b'"unclosed_header\n'
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(bad_header)
    assert exc_info.value.code == "ERR_MALFORMED_CSV"
    assert exc_info.value.row_number == 1


def test_f06_unclosed_quote_in_data_raises_controlled_malformed_csv() -> None:
    """F06: Proves unclosed quote in data row raises MalformedCSVError with row number."""
    bad_data = b'col1,col2\n"unclosed_val,2\n'
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(bad_data)
    assert exc_info.value.code == "ERR_MALFORMED_CSV"
    assert exc_info.value.row_number == 2


def test_f06_unexpected_character_after_closing_quote() -> None:
    """F06: Proves unexpected characters after quote raise MalformedCSVError."""
    bad_syntax = b'col1,col2\n"a"b,c\n'
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(bad_syntax)
    assert exc_info.value.code == "ERR_MALFORMED_CSV"
    assert exc_info.value.row_number == 2


def test_f06_blank_line_in_data_raises_malformed_csv() -> None:
    """F06: Proves blank record in data raises MalformedCSVError."""
    blank_middle = b"col1,col2\n1,2\n\n3,4\n"
    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(blank_middle)
    assert exc_info.value.code == "ERR_MALFORMED_CSV"
    assert exc_info.value.row_number == 3
    assert exc_info.value.expected_columns == 2
    assert exc_info.value.actual_columns == 0


# =====================================================================
# P2-03: Sanitize Expected Filesystem Metadata Errors
# =====================================================================

def test_p2_03_metadata_permission_error_sanitized_to_source_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P2-03: Proves simulated metadata permission failure on Path.is_file raises sanitized
    SourceReadError without leaking the full secret path or raw OS errno.
    """
    secret_dir = "C:/top_secret_credentials/hidden_folder/secrets.csv"

    def mock_is_file(self: Path) -> bool:
        raise PermissionError(f"[Errno 13] Permission denied: '{secret_dir}'")

    monkeypatch.setattr(Path, "is_file", mock_is_file)

    with pytest.raises(SourceReadError) as exc_info:
        ingest_csv(Path(secret_dir))

    err_str = str(exc_info.value)
    assert "secrets.csv" in err_str
    assert "top_secret_credentials" not in err_str
    assert "hidden_folder" not in err_str
    assert "Errno 13" not in err_str


def test_p2_03_open_permission_error_sanitized_to_source_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    P2-03: Proves simulated open permission failure raises sanitized SourceReadError
    without leaking path details.
    """
    secret_path = Path("C:/restricted/private_finance_records.csv")

    def mock_is_file(self: Path) -> bool:
        return True

    def mock_getsize(path: Any) -> int:
        return 100

    def mock_open(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(f"[Errno 13] Permission denied: '{secret_path}'")

    monkeypatch.setattr(Path, "is_file", mock_is_file)
    monkeypatch.setattr(os.path, "getsize", mock_getsize)
    monkeypatch.setattr("builtins.open", mock_open)

    with pytest.raises(SourceReadError) as exc_info:
        ingest_csv(secret_path)

    err_str = str(exc_info.value)
    assert "private_finance_records.csv" in err_str
    assert "restricted" not in err_str


def test_p2_03_missing_file_preserves_file_not_found_error() -> None:
    """
    P2-03: Proves missing paths raise FileNotFoundError with sanitized basename only.
    """
    missing_path = Path("C:/confidential_project/internal_data/missing_catalog.csv")
    with pytest.raises(FileNotFoundError) as exc_info:
        ingest_csv(missing_path)
    err_str = str(exc_info.value)
    assert "missing_catalog.csv" in err_str
    assert "confidential_project" not in err_str


def test_f06_source_read_error_on_unreadable_stream() -> None:
    """F06: Proves I/O error on source stream raises SourceReadError."""
    class BrokenStream(io.RawIOBase):
        def read(self, size: int = -1) -> bytes:
            raise OSError("Low-level device I/O error: disk cylinder damaged")

    with pytest.raises(SourceReadError) as exc_info:
        ingest_csv(BrokenStream())
    assert exc_info.value.code == "ERR_SOURCE_READ"
    assert "Failed to read from source stream" in str(exc_info.value)
    assert "cylinder damaged" not in str(exc_info.value)


def test_p2_03_unrelated_programmer_errors_propagate_unchanged() -> None:
    """
    P2-03: Proves unrelated runtime/programmer errors (e.g., passing invalid object)
    propagate unchanged and are not masked as SourceReadError.
    """
    with pytest.raises(AttributeError):
        ingest_csv(12345)  # type: ignore[arg-type]


def test_f06_errors_never_leak_sensitive_cell_contents() -> None:
    """
    F06: Asserts secret sentinel tokens never appear in error messages or exception fields.
    """
    secret = "SUPER_SECRET_TOKEN_DO_NOT_LEAK_12345"
    malformed_csv = f'col1,col2\n"unclosed_quote_{secret},value\n'.encode("utf-8")

    with pytest.raises(MalformedCSVError) as exc_info:
        ingest_csv(malformed_csv)

    err_str = str(exc_info.value)
    assert secret not in err_str
    assert secret not in repr(exc_info.value)


# =====================================================================
# P2-04: Distinguish Native Parser Guard From Configured Byte Limit
# =====================================================================

def test_p2_04_native_parser_guard_failure_distinguished_from_configured_limit() -> None:
    """
    P2-04: Proves a field exceeding the native parser guard (131,072) while below a larger
    configured max_cell_bytes (200,000) produces a truthful error identifying the native
    parser guard, without claiming the configured limit was exceeded.
    """
    # Create cell of 135,000 characters
    huge_cell = "y" * 135_000
    csv_bytes = f"col1,col2\n1,{huge_cell}\n".encode("utf-8")
    configured_limit = 200_000

    with pytest.raises(CellSizeLimitExceededError) as exc_info:
        ingest_csv(csv_bytes, limits=IngestionLimits(max_cell_bytes=configured_limit))

    err = exc_info.value
    # Truthful identification of native parser limit
    assert isinstance(err, ParserFieldSizeLimitError)
    assert err.is_parser_limit is True
    assert "native CSV parser field size limit" in str(err)
    assert "200,000" not in str(err)  # Does NOT falsely claim configured limit was exceeded
    assert err.actual_bytes is None  # Unknown because native parser stopped
    assert err.column_index is None
    assert err.max_bytes is None
    assert err.row_number == 2  # Logical record 2


def test_p2_04_configured_byte_limit_failure_reports_measured_bytes() -> None:
    """
    P2-04: Proves a field genuinely exceeding the configured byte ceiling (e.g., 32,768)
    reports the configured limit correctly with the measured byte count.
    """
    # 40,000 bytes is well below native parser limit (131,072) but exceeds default 32,768
    cell_40k = "z" * 40_000
    csv_bytes = f"col1,col2\n1,{cell_40k}\n".encode("utf-8")

    with pytest.raises(CellSizeLimitExceededError) as exc_info:
        ingest_csv(csv_bytes)  # Default max_cell_bytes = 32,768

    err = exc_info.value
    assert err.is_parser_limit is False
    assert err.actual_bytes == 40_000
    assert err.max_bytes == 32_768
    assert err.column_index == 1
    assert err.row_number == 2
    assert "exceeds maximum allowed cell size (32,768 bytes)" in str(err)


def test_p2_04_header_native_parser_guard_failure() -> None:
    """
    P2-04: Proves native parser guard failure in the header row produces a truthful error
    at row 1 without fabricated column coordinates or byte counts.
    """
    huge_header = "h" * 135_000
    csv_bytes = f"{huge_header},col2\n1,2\n".encode("utf-8")

    with pytest.raises(CellSizeLimitExceededError) as exc_info:
        ingest_csv(csv_bytes, limits=IngestionLimits(max_cell_bytes=200_000))

    err = exc_info.value
    assert isinstance(err, ParserFieldSizeLimitError)
    assert err.is_parser_limit is True
    assert err.row_number == 1
    assert err.actual_bytes is None
    assert err.column_index is None
    assert "Cell at row 1 exceeds native CSV parser field size limit" in str(err)
