"""
Integration Test Suite for Data Intelligence Lab - Phase 3 Inference Policy & Ingestion
Validates InferencePolicy configuration, pre-acquisition validation, post-normalization resolution,
literal text preservation, collision handling, Unicode names, and decimal-context independence.
"""

import decimal
from io import BytesIO
import pytest

from core import (
    InferencePolicy,
    IngestionLimits,
    NumericRangeError,
    ingest_csv,
)
from core.types import (
    DECISION_ALL_NULL,
    DECISION_BOOLEAN,
    DECISION_DATE,
    DECISION_DATETIME,
    DECISION_FLOAT,
    DECISION_FLOAT_PRECISION_LOSS,
    DECISION_INTEGER,
    DECISION_LEADING_ZERO,
    DECISION_NO_UNIFORM_TYPE,
    DECISION_PRESERVE_AS_TEXT,
)


# =====================================================================
# P3-A8: Policy Configuration & Pre-Acquisition Validation
# =====================================================================

def test_inference_policy_runtime_validation() -> None:
    """InferencePolicy validates preserve_as_text types at runtime."""
    # Valid constructions
    p1 = InferencePolicy()
    assert p1.preserve_as_text == frozenset()

    p2 = InferencePolicy(preserve_as_text={"col_a", "col_b"})
    assert isinstance(p2.preserve_as_text, frozenset)
    assert p2.preserve_as_text == frozenset({"col_a", "col_b"})

    p3 = InferencePolicy(preserve_as_text=["col_c"])
    assert p3.preserve_as_text == frozenset({"col_c"})

    # Invalid constructions
    with pytest.raises(TypeError):
        InferencePolicy(preserve_as_text="not_a_collection")  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        InferencePolicy(preserve_as_text={123, "valid"})  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        InferencePolicy(preserve_as_text=123)  # type: ignore[arg-type]


def test_pre_acquisition_validation_fails_fast() -> None:
    """ingest_csv validates limits and inference_policy types before source acquisition."""
    csv_bytes = b"id,val\n1,10\n"

    with pytest.raises(TypeError, match="inference_policy must be an instance of InferencePolicy"):
        ingest_csv(csv_bytes, inference_policy={"not": "a policy"})  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="limits must be an instance of IngestionLimits"):
        ingest_csv(csv_bytes, limits={"not": "limits"})  # type: ignore[arg-type]


# =====================================================================
# P3-A8: Post-Normalization Column Resolution
# =====================================================================

def test_unknown_column_in_preserve_as_text_raises_exact_value_error() -> None:
    """Specifying a column in preserve_as_text that does not exist in normalized headers raises exact ValueError."""
    csv_bytes = b"id,user_name,score\n1,alice,95\n"
    policy = InferencePolicy(preserve_as_text={"non_existent_column"})

    with pytest.raises(ValueError, match=r"^Unknown normalized column in preserve_as_text\.$"):
        ingest_csv(csv_bytes, inference_policy=policy)


def test_case_sensitive_resolution_of_normalized_names() -> None:
    """preserve_as_text matches normalized unique column names case-sensitively."""
    # Header 'User Name' normalizes to 'user_name'
    csv_bytes = b"User Name,Score\nalice,95\n"
    
    # Passing raw un-normalized 'User Name' fails because names are resolved post-normalization
    with pytest.raises(ValueError, match=r"^Unknown normalized column in preserve_as_text\.$"):
        ingest_csv(csv_bytes, inference_policy=InferencePolicy(preserve_as_text={"User Name"}))

    # Passing normalized 'user_name' succeeds
    policy = InferencePolicy(preserve_as_text={"user_name"})
    result = ingest_csv(csv_bytes, inference_policy=policy)
    meta_map = {m.normalized_name: m for m in result.column_metadata}
    assert meta_map["user_name"].inferred_type == "string"
    assert meta_map["user_name"].inference_reason == DECISION_PRESERVE_AS_TEXT


def test_collision_order_resolution_with_preserve_as_text() -> None:
    """When headers collide, collision suffix names (e.g. id_1) can be targeted in preserve_as_text."""
    # Two columns named 'id' collide -> normalized to 'id' and 'id_1'
    csv_bytes = b"id,ID\n001,002\n"
    policy = InferencePolicy(preserve_as_text={"id_1"})
    result = ingest_csv(csv_bytes, inference_policy=policy)

    meta_map = {m.normalized_name: m for m in result.column_metadata}
    # id was not in preserve_as_text: has leading zeros -> string with LEADING_ZERO
    assert meta_map["id"].inferred_type == "string"
    assert meta_map["id"].inference_reason == DECISION_LEADING_ZERO

    # id_1 was in preserve_as_text -> string with PRESERVE_AS_TEXT
    assert meta_map["id_1"].inferred_type == "string"
    assert meta_map["id_1"].inference_reason == DECISION_PRESERVE_AS_TEXT
    assert result.records[0]["id_1"] == "002"


def test_unicode_column_name_resolution() -> None:
    """Columns with Unicode names resolve correctly against preserve_as_text."""
    # 'pr\xc3\xa9f\xc3\xa9rence' normalizes via NFKC and ascii slug or normalization
    csv_bytes = "donnée,valeur\n123,456\n".encode("utf-8")
    
    # Check normalized name
    res_default = ingest_csv(csv_bytes)
    norm_names = [m.normalized_name for m in res_default.column_metadata]
    assert len(norm_names) == 2
    target_col = norm_names[0]

    policy = InferencePolicy(preserve_as_text={target_col})
    res_policy = ingest_csv(csv_bytes, inference_policy=policy)
    meta_map = {m.normalized_name: m for m in res_policy.column_metadata}
    assert meta_map[target_col].inferred_type == "string"
    assert meta_map[target_col].inference_reason == DECISION_PRESERVE_AS_TEXT
    assert res_policy.records[0][target_col] == "123"


# =====================================================================
# P3-A8: Literal Preservation Behavior
# =====================================================================

def test_literal_preservation_bypasses_null_and_preserves_raw_strings() -> None:
    """
    Literal columns preserve exact parsed strings, bypassing null-value recognition,
    preserving whitespace, empty strings, signs, and leading zeros with null_count=0.
    """
    # Includes:
    # 1. Whitespace: '  padded  '
    # 2. Empty string: ''
    # 3. Null-like tokens: 'NA', 'null', '-'
    # 4. Signs & leading zeros: '+007', '-0.0'
    csv_text = (
        "literal_col,normal_col\n"
        "  padded  ,10\n"
        ",20\n"
        "NA,30\n"
        "null,40\n"
        "-,50\n"
        "+007,60\n"
        "-0.0,70\n"
    )
    policy = InferencePolicy(preserve_as_text={"literal_col"})
    result = ingest_csv(csv_text.encode("utf-8"), inference_policy=policy)

    meta_map = {m.normalized_name: m for m in result.column_metadata}
    assert meta_map["literal_col"].inferred_type == "string"
    assert meta_map["literal_col"].inference_reason == DECISION_PRESERVE_AS_TEXT
    assert meta_map["literal_col"].inference_policy_version == "1.0"

    # Profile assertions: literal column null_count is 0
    prof_map = {c.name: c for c in result.profile.columns}
    assert prof_map["literal_col"].null_count == 0
    assert prof_map["literal_col"].null_ratio == 0.0

    # Values in records match exact strings without stripping
    expected_values = [
        "  padded  ",
        "",
        "NA",
        "null",
        "-",
        "+007",
        "-0.0",
    ]
    actual_values = [r["literal_col"] for r in result.records]
    assert actual_values == expected_values

    # normal_col was properly typed as integer
    assert meta_map["normal_col"].inferred_type == "integer"
    assert meta_map["normal_col"].inference_reason == DECISION_INTEGER
    assert [r["normal_col"] for r in result.records] == [10, 20, 30, 40, 50, 60, 70]


# =====================================================================
# P3-A9: Decision Metadata & Header-Only Input
# =====================================================================

def test_header_only_csv_receives_all_null_reasons() -> None:
    """CSV with header row but 0 data rows receives ALL_NULL reason for all columns."""
    csv_bytes = b"col_a,col_b,col_c\n"
    result = ingest_csv(csv_bytes)

    assert result.profile.row_count == 0
    assert result.profile.column_count == 3
    for meta in result.column_metadata:
        assert meta.inferred_type == "string"
        assert meta.inference_reason == DECISION_ALL_NULL
        assert meta.inference_policy_version == "1.0"


def test_mixed_columns_decision_codes() -> None:
    """Confirms decision codes for a dataset with various data types."""
    csv_text = (
        "int_col,float_col,bool_col,date_col,dt_col,text_col\n"
        "10,1.5,true,2024-01-01,2024-01-01T12:00:00,hello\n"
        "20,2.5,false,2024-01-02,2024-01-02T12:00:00,world\n"
    )
    result = ingest_csv(csv_text.encode("utf-8"))
    meta_map = {m.normalized_name: m for m in result.column_metadata}

    assert meta_map["int_col"].inference_reason == DECISION_INTEGER
    assert meta_map["float_col"].inference_reason == DECISION_FLOAT
    assert meta_map["bool_col"].inference_reason == DECISION_BOOLEAN
    assert meta_map["date_col"].inference_reason == DECISION_DATE
    assert meta_map["dt_col"].inference_reason == DECISION_DATETIME
    assert meta_map["text_col"].inference_reason == DECISION_NO_UNIFORM_TYPE


# =====================================================================
# Independence from Global Decimal Context
# =====================================================================

def test_decimal_context_independence_during_ingestion() -> None:
    """Ingestion numeric precision checks are immune to modified decimal.getcontext().prec."""
    # Under extreme low precision (prec=2), Decimal('1.2345') arithmetic rounds,
    # but exact string equality Decimal(str(f)) == d must NOT be affected.
    orig_prec = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 2
        csv_bytes = b"score\n1.2345\n2.3456\n"
        result = ingest_csv(csv_bytes)
        meta = result.column_metadata[0]
        assert meta.inferred_type == "float"
        assert meta.inference_reason == DECISION_FLOAT
        assert result.records[0]["score"] == 1.2345
    finally:
        decimal.getcontext().prec = orig_prec


# =====================================================================
# End-to-End Profiling NumericRangeError
# =====================================================================

def test_end_to_end_numeric_range_error() -> None:
    """Ingestion of numeric column causing calculation overflow raises sanitized NumericRangeError."""
    # Two large integers that overflow float64 variance calculation
    v = "1" + "0" * 160
    csv_bytes = f"col_a,col_b\n1,{v}\n2,-{v}\n".encode("utf-8")
    with pytest.raises(NumericRangeError) as exc_info:
        ingest_csv(csv_bytes)
    err = exc_info.value
    assert err.code == "ERR_NUMERIC_RANGE"
    assert err.column_index == 1  # zero-based index of col_b
    assert err.operation == "variance"
    assert "column 1" in err.message
    assert v not in err.message


# =====================================================================
# Phase 3 Targeted Remediation Tests (P3-V01, P3-V03, P3-V06)
# =====================================================================

def test_p3_v01_csv_ingestion_scientific_notation_integrity() -> None:
    """CSV ingestion of 1e23 preserves string to prevent integer corruption; 1e20 parses as exact float."""
    # 1e23 cannot fit in float64 without integer corruption -> falls back to string
    res_23 = ingest_csv(b"v\n1e23\n")
    assert res_23.column_metadata[0].inferred_type == "string"
    assert res_23.column_metadata[0].inference_reason == DECISION_FLOAT_PRECISION_LOSS
    assert res_23.records[0]["v"] == "1e23"

    # 1e20 fits in float64 without integer corruption -> parsed as float
    res_20 = ingest_csv(b"v\n1e20\n")
    assert res_20.column_metadata[0].inferred_type == "float"
    assert res_20.column_metadata[0].inference_reason == DECISION_FLOAT
    assert res_20.records[0]["v"] == 1e20


def test_p3_v03_csv_ingestion_odd_length_median_overflow() -> None:
    """CSV ingestion of column causing odd-length median overflow raises sanitized NumericRangeError without chaining."""
    h = 10**400
    csv_bytes = f"col_a,col_b\n1,{-2 * h}\n2,{h}\n3,{h}\n".encode("utf-8")
    with pytest.raises(NumericRangeError) as exc_info:
        ingest_csv(csv_bytes)
    err = exc_info.value
    assert err.code == "ERR_NUMERIC_RANGE"
    assert err.column_index == 1
    assert err.operation == "median"
    assert err.__cause__ is None
    assert "column 1" in err.message
    assert str(h) not in err.message


def test_p3_v06_policy_rejects_empty_names_immediately() -> None:
    """InferencePolicy rejects empty and whitespace-only column names during construction."""
    with pytest.raises(ValueError, match="Column names in preserve_as_text cannot be empty."):
        InferencePolicy([""])

    with pytest.raises(ValueError, match="Column names in preserve_as_text cannot be empty."):
        InferencePolicy(["   "])

    with pytest.raises(ValueError, match="Column names in preserve_as_text cannot be empty."):
        InferencePolicy(preserve_as_text={"valid_col", ""})


def test_p3_v06_zero_source_acquisition_observable_stream() -> None:
    """Invalid policy fails fast before source acquisition, guaranteeing zero stream reads."""
    class TrackingStream(BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.read_call_count = 0

        def read(self, *args, **kwargs) -> bytes:
            self.read_call_count += 1
            return super().read(*args, **kwargs)

    stream = TrackingStream(b"col_a,col_b\n1,2\n")

    # 1. Invalid policy member type
    with pytest.raises(TypeError):
        ingest_csv(stream, inference_policy=InferencePolicy([123]))  # type: ignore[list-item]
    assert stream.read_call_count == 0, "Expected zero read calls on stream"

    # 2. Empty member in policy
    with pytest.raises(ValueError):
        ingest_csv(stream, inference_policy=InferencePolicy([""]))
    assert stream.read_call_count == 0, "Expected zero read calls on stream"


def test_p3_v06_zero_source_acquisition_filesystem_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid policy fails fast inside ingest_csv before opening or reading any filesystem path."""
    import builtins
    import pathlib

    open_calls = []
    orig_open = builtins.open

    def spied_open(*args, **kwargs):
        open_calls.append(args)
        return orig_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", spied_open)
    fake_path = pathlib.Path("some_fake_file.csv")

    # 1. Invalid policy type rejected inside ingest_csv before open
    with pytest.raises(TypeError, match="inference_policy must be an instance of InferencePolicy"):
        ingest_csv(fake_path, inference_policy="invalid_policy_type")  # type: ignore[arg-type]
    assert len(open_calls) == 0, f"Filesystem path was opened despite invalid policy type! Calls: {open_calls}"

    # 2. Policy object with empty string member rejected inside ingest_csv before open
    policy = object.__new__(InferencePolicy)
    object.__setattr__(policy, "preserve_as_text", frozenset({""}))
    with pytest.raises(ValueError, match="Column names in preserve_as_text cannot be empty."):
        ingest_csv(fake_path, inference_policy=policy)
    assert len(open_calls) == 0, f"Filesystem path was opened despite invalid policy member! Calls: {open_calls}"
