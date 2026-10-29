"""
Unit Test Suite for Data Intelligence Lab - Phase 3 Type Inference
Validates conservative numeric inference, decimal exactness, temporal parsing,
awareness rules, submicrosecond protection, and row-permutation determinism.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import List
import pytest

from core.type_inference import (
    has_significant_leading_zero,
    infer_column_type,
    parse_boolean,
    parse_date,
    parse_datetime,
    parse_float,
    parse_integer,
)
from core.types import (
    DECISION_ALL_NULL,
    DECISION_BOOLEAN,
    DECISION_DATE,
    DECISION_DATETIME,
    DECISION_FLOAT,
    DECISION_FLOAT_PRECISION_LOSS,
    DECISION_FLOAT_UNDERFLOW,
    DECISION_INTEGER,
    DECISION_LEADING_ZERO,
    DECISION_MIXED_AWARENESS,
    DECISION_MIXED_TEMPORAL_KINDS,
    DECISION_NO_UNIFORM_TYPE,
    DECISION_NON_FINITE_FLOAT,
    DECISION_PRESERVE_AS_TEXT,
    DECISION_SUBMICROSECOND_PRECISION,
    ExecutionDeadline,
    InferenceResult,
    ProcessingTimeoutError,
)


# =====================================================================
# P3-A1 & P3-A2: Conservative Numeric Float Acceptance & Rejection
# =====================================================================

def test_p3_a1_valid_floats_accepted() -> None:
    """Valid floating point numbers that round-trip exactly to Decimal are accepted."""
    col = ["1.5", "0.1", "100.0", "1e-5", "1e20", "-42.5"]
    res = infer_column_type(col)
    assert res.inferred_type == "float"
    assert res.inference_reason == DECISION_FLOAT
    assert res.null_count == 0
    assert res.typed_values == [1.5, 0.1, 100.0, 1e-5, 1e20, -42.5]


def test_p3_a1_precision_losing_floats_rejected() -> None:
    """Floats that lose decimal precision during IEEE-754 representation fall back to string."""
    # 9007199254740993.0 cannot be represented in float64 without loss
    # 1.00000000000000012345 loses fractional precision
    col = ["1.5", "9007199254740993.0", "2.5"]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_FLOAT_PRECISION_LOSS
    assert res.typed_values == col  # original strings preserved without stripping


def test_p3_a2_non_finite_floats_rejected() -> None:
    """Non-finite float representations (inf, -inf, nan) fall back to string."""
    col = ["1.5", "inf", "2.5"]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_NON_FINITE_FLOAT
    assert res.typed_values == col

    col_nan = ["1.5", "-inf", "3.0"]
    res_nan = infer_column_type(col_nan)
    assert res_nan.inferred_type == "string"
    assert res_nan.inference_reason == DECISION_NON_FINITE_FLOAT


def test_p3_a2_float_underflow_rejected() -> None:
    """Nonzero values that underflow to zero float fall back to string."""
    col = ["1.5", "1e-400", "2.5"]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_FLOAT_UNDERFLOW
    assert res.typed_values == col


def test_p3_a4_negative_zero_preserved() -> None:
    """Negative zero float string '-0.0' preserves negative sign."""
    col = ["-0.0", "0.0", "1.5"]
    res = infer_column_type(col)
    assert res.inferred_type == "float"
    assert res.inference_reason == DECISION_FLOAT
    # Test sign bit via math.copysign
    assert math.copysign(1.0, res.typed_values[0]) == -1.0
    assert math.copysign(1.0, res.typed_values[1]) == 1.0


# =====================================================================
# P3-A3: Exact Integer & Leading Zero Preservation
# =====================================================================

def test_p3_a3_valid_integers_accepted() -> None:
    """Valid integer strings without leading zeros are inferred as integer."""
    col = ["1", "42", "-100", "+500", "0"]
    res = infer_column_type(col)
    assert res.inferred_type == "integer"
    assert res.inference_reason == DECISION_INTEGER
    assert res.typed_values == [1, 42, -100, 500, 0]


def test_p3_a3_leading_zeros_fall_back_to_string() -> None:
    """Values with significant leading zeros remain strings to prevent loss of leading zeros."""
    col = ["0123", "007", "+012"]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_LEADING_ZERO
    assert res.typed_values == col


# =====================================================================
# P3-A5, P3-A6, P3-A7: Temporal Validation, Awareness & Precision
# =====================================================================

def test_p3_a5_date_parsing_and_calendar_validation() -> None:
    """Valid dates parsed as datetime.date objects; invalid dates fall back to string."""
    valid_col = ["2024-01-15", "2024-02-29", "2024-12-31"]  # 2024 is a leap year
    res = infer_column_type(valid_col)
    assert res.inferred_type == "date"
    assert res.inference_reason == DECISION_DATE
    assert res.typed_values == [date(2024, 1, 15), date(2024, 2, 29), date(2024, 12, 31)]

    # 2023 is not a leap year -> 2023-02-29 is invalid calendar date
    invalid_col = ["2023-01-15", "2023-02-29"]
    res_inv = infer_column_type(invalid_col)
    assert res_inv.inferred_type == "string"


def test_p3_a5_naive_datetime_parsing() -> None:
    """Uniform naive datetimes parsed as naive datetime objects."""
    col = ["2024-01-01T12:00:00", "2024-06-15 18:30:45", "2024-12-31T23:59:59.123456"]
    res = infer_column_type(col)
    assert res.inferred_type == "datetime"
    assert res.inference_reason == DECISION_DATETIME
    for dt in res.typed_values:
        assert isinstance(dt, datetime)
        assert dt.tzinfo is None


def test_p3_a6_aware_datetime_preserves_per_value_offsets() -> None:
    """Uniform aware datetimes preserve valid per-value offsets without UTC normalization."""
    col = [
        "2024-01-01T12:00:00+02:00",
        "2024-01-01T10:00:00Z",
        "2024-01-01T05:00:00-05:00",
    ]
    res = infer_column_type(col)
    assert res.inferred_type == "datetime"
    assert res.inference_reason == DECISION_DATETIME
    dt1, dt2, dt3 = res.typed_values
    assert dt1.utcoffset() == timedelta(hours=2)
    assert dt2.utcoffset() == timedelta(0)
    assert dt3.utcoffset() == timedelta(hours=-5)


def test_p3_a6_mixed_awareness_falls_back_to_string() -> None:
    """Mixing timezone-naive and timezone-aware datetimes falls back to string."""
    col = [
        "2024-01-01T12:00:00",       # naive
        "2024-01-01T12:00:00+02:00", # aware
    ]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_MIXED_AWARENESS
    assert res.typed_values == col


def test_p3_a7_mixed_temporal_kinds_falls_back_to_string() -> None:
    """Mixing date-only and datetime strings in the same column falls back to string."""
    col = [
        "2024-01-01",                # date
        "2024-01-01T12:00:00",       # datetime
    ]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_MIXED_TEMPORAL_KINDS
    assert res.typed_values == col


def test_p3_a7_submicrosecond_precision_falls_back_to_string() -> None:
    """Timestamps with >6 fractional digits fall back to string to prevent lossy truncation."""
    col = [
        "2024-01-01T12:00:00.123456",   # 6 digits: microsecond (supported)
        "2024-01-01T12:00:00.1234567",  # 7 digits: submicrosecond (lossy)
    ]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_SUBMICROSECOND_PRECISION
    assert res.typed_values == col


# =====================================================================
# Row-Permutation Determinism Gate
# =====================================================================

def test_row_permutation_determinism_gate() -> None:
    """
    Reason code selection must be 100% deterministic and invariant under row permutations.
    When a uniformly numeric column contains multiple failure conditions, the highest precedence reason
    must be chosen regardless of the row order in the input.
    """
    # Contains:
    # 1. Leading zero ('0123') -> precedence 1
    # 2. Precision loss ('9007199254740993.0') -> precedence 2
    # 3. Float underflow ('1e-400') -> precedence 3
    items = ["0123", "9007199254740993.0", "1e-400"]

    from itertools import permutations
    for perm in permutations(items):
        res = infer_column_type(list(perm))
        assert res.inferred_type == "string"
        assert res.inference_reason == DECISION_LEADING_ZERO, (
            f"Failed on permutation {perm}: expected {DECISION_LEADING_ZERO}, got {res.inference_reason}"
        )


def test_row_permutation_determinism_mixed_families() -> None:
    """Incompatible candidate families (numeric + temporal) evaluate to NO_UNIFORM_TYPE across all row permutations."""
    items = ["1e-400", "2024-01-01T12:00:00.1234567"]
    from itertools import permutations
    for perm in permutations(items):
        res = infer_column_type(list(perm))
        assert res.inferred_type == "string"
        assert res.inference_reason == DECISION_NO_UNIFORM_TYPE
        assert res.typed_values == list(perm)
        assert res.null_count == 0


# =====================================================================
# General Text & Empty Cases
# =====================================================================

def test_general_text_column() -> None:
    """Arbitrary text without candidate primitive matches receives NO_UNIFORM_TYPE."""
    col = ["Alice", "Bob", "Charlie"]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_NO_UNIFORM_TYPE
    assert res.typed_values == col


def test_all_null_and_empty_columns() -> None:
    """All null and empty sequences receive ALL_NULL."""
    res_null = infer_column_type(["", "NA", "null", None if False else "-"])
    assert res_null.inferred_type == "string"
    assert res_null.inference_reason == DECISION_ALL_NULL
    assert res_null.null_count == 4
    assert res_null.typed_values == [None, None, None, None]

    res_empty = infer_column_type([])
    assert res_empty.inferred_type == "string"
    assert res_empty.inference_reason == DECISION_ALL_NULL
    assert res_empty.null_count == 0
    assert res_empty.typed_values == []


def test_literal_override_bypasses_inference() -> None:
    """is_literal=True bypasses all type inference and null parsing."""
    col = ["", "NA", "0123", "1.5"]
    res = infer_column_type(col, is_literal=True)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_PRESERVE_AS_TEXT
    assert res.null_count == 0
    assert res.typed_values == ["", "NA", "0123", "1.5"]


# =====================================================================
# Backward Compatibility Tuple Contract & Deadline Expiry
# =====================================================================

def test_inference_result_tuple_compatibility() -> None:
    """InferenceResult must support 3-item unpacking and len() == 3 for backward compatibility."""
    res = infer_column_type(["true", "false", "true"])
    inferred_type, typed_vals, null_cnt = res
    assert inferred_type == "boolean"
    assert typed_vals == [True, False, True]
    assert null_cnt == 0
    assert len(res) == 3
    assert res[0] == "boolean"
    assert res.inference_reason == DECISION_BOOLEAN


def test_inference_deadline_expiry_halts_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """ExecutionDeadline expiry raises ProcessingTimeoutError during inference."""
    import time
    start_time = 1000.0
    timeout_seconds = 2.0
    simulated_time = start_time
    calls = 0

    def mock_monotonic() -> float:
        nonlocal calls, simulated_time
        calls += 1
        if calls >= 3:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)
    with pytest.raises(ProcessingTimeoutError):
        infer_column_type(["1", "2", "3", "4", "5"], deadline=deadline, poll_interval=1)


# =====================================================================
# Phase 3 Targeted Remediation Tests (P3-V01, P3-V02, P3-V04, P3-V05)
# =====================================================================

def test_p3_v01_scientific_notation_numeric_integrity() -> None:
    """Every mathematically integral decimal candidate must be verified without exponent exemption."""
    cases = [
        # Candidate, expected inferred_type, expected typed_value, expected reason
        ("1e23", "string", "1e23", DECISION_FLOAT_PRECISION_LOSS),
        ("1E23", "string", "1E23", DECISION_FLOAT_PRECISION_LOSS),
        ("100000000000000000000000", "integer", 100000000000000000000000, DECISION_INTEGER),
        ("100000000000000000000000.0", "string", "100000000000000000000000.0", DECISION_FLOAT_PRECISION_LOSS),
        ("1e20", "float", 1e20, DECISION_FLOAT),
        ("1e21", "float", 1e21, DECISION_FLOAT),
        ("1.5e2", "float", 150.0, DECISION_FLOAT),
        ("1.5e-1", "float", 0.15, DECISION_FLOAT),
        ("-1e23", "string", "-1e23", DECISION_FLOAT_PRECISION_LOSS),
    ]

    for val_str, exp_type, exp_val, exp_reason in cases:
        res = infer_column_type([val_str])
        assert res.inferred_type == exp_type, f"Failed type for {val_str}: got {res.inferred_type}"
        assert res.typed_values[0] == exp_val, f"Failed value for {val_str}: got {res.typed_values[0]}"
        assert res.inference_reason == exp_reason, f"Failed reason for {val_str}: got {res.inference_reason}"


def test_p3_v01_equivalent_mathematical_representations() -> None:
    """Equivalent representations must not silently produce divergent incorrect values."""
    # Both 100000000000000000000000.0 and 1e23 cannot fit in float64 without loss
    res_dec = infer_column_type(["100000000000000000000000.0"])
    res_sci = infer_column_type(["1e23"])
    assert res_dec.inferred_type == "string"
    assert res_sci.inferred_type == "string"
    assert res_dec.inference_reason == DECISION_FLOAT_PRECISION_LOSS
    assert res_sci.inference_reason == DECISION_FLOAT_PRECISION_LOSS

    # Both 100000000000000000000.0 and 1e20 fit safely in float64
    res_dec_safe = infer_column_type(["100000000000000000000.0"])
    res_sci_safe = infer_column_type(["1e20"])
    assert res_dec_safe.inferred_type == "float"
    assert res_sci_safe.inferred_type == "float"
    assert res_dec_safe.typed_values[0] == 1e20
    assert res_sci_safe.typed_values[0] == 1e20


def test_p3_v02_invalid_timezone_offsets_rejected() -> None:
    """Invalid timezone offset minutes (>59) and hours (>23) must be rejected without silent normalization."""
    invalid_samples = [
        "2024-01-01T00:00:00+01:60",
        "2024-01-01T00:00:00-00:99",
        "2024-01-01T00:00:00+24:00",
        "2024-01-01T00:00:00-25:00",
    ]
    for s in invalid_samples:
        assert parse_datetime(s) is None, f"Expected {s} to be rejected"
        res = infer_column_type([s])
        assert res.inferred_type == "string"
        assert res.inference_reason == DECISION_NO_UNIFORM_TYPE


def test_p3_v02_fractional_second_precision_rules() -> None:
    """Fractional seconds <=6 digits or >6 digits with only trailing zeros are valid; >6 digits with non-zero info reject."""
    valid_samples = [
        ("2024-01-01T00:00:00.123456", 123456),
        ("2024-01-01T00:00:00.123456000", 123456),
        ("2024-01-01T00:00:00.123456000000", 123456),
    ]
    for s, exp_micro in valid_samples:
        parsed = parse_datetime(s)
        assert parsed is not None, f"Expected {s} to parse"
        assert parsed.microsecond == exp_micro
        res = infer_column_type([s])
        assert res.inferred_type == "datetime"
        assert res.inference_reason == DECISION_DATETIME

    # Submicrosecond precision with non-zero digits beyond microsecond
    submicro_samples = [
        "2024-01-01T00:00:00.123456001",
        "2024-01-01T00:00:00.1234567",
    ]
    for s in submicro_samples:
        assert parse_datetime(s) is None
        res = infer_column_type([s])
        assert res.inferred_type == "string"
        assert res.inference_reason == DECISION_SUBMICROSECOND_PRECISION


def test_p3_v04_deadline_expires_during_timezone_awareness_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deadline expiring during timezone-awareness scanning must raise ProcessingTimeoutError."""
    import time
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time

    parse_count = 0
    import core.type_inference
    orig_parse_datetime = core.type_inference.parse_datetime

    def tracked_parse_datetime(v: str):
        nonlocal parse_count
        parse_count += 1
        return orig_parse_datetime(v)

    monkeypatch.setattr(core.type_inference, "parse_datetime", tracked_parse_datetime)

    tz_checks = 0
    def mock_monotonic() -> float:
        nonlocal tz_checks, simulated_time
        # Only advance clock once datetime conversion is complete (2000 in matches_all + 2000 in convert_column = 4000)
        # and timezone awareness scan begins
        if parse_count >= 4000:
            tz_checks += 1
            if tz_checks >= 3:
                simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    # 2,000 valid datetimes with poll_interval=100
    values = ["2024-01-01T12:00:00Z"] * 2000
    with pytest.raises(ProcessingTimeoutError):
        infer_column_type(values, deadline=deadline, poll_interval=100)

    assert parse_count >= 4000, f"parse_count was {parse_count}, expected >= 4000"
    assert tz_checks >= 3, f"tz_checks was {tz_checks}, expected >= 3"


def test_p3_v04_deadline_expires_during_literal_preservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deadline expiring during literal column copy must raise ProcessingTimeoutError."""
    import time
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    check_count = 0

    def mock_monotonic() -> float:
        nonlocal check_count, simulated_time
        check_count += 1
        if check_count >= 10:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    values = ["item"] * 2000
    with pytest.raises(ProcessingTimeoutError):
        infer_column_type(values, deadline=deadline, poll_interval=100, is_literal=True)


def test_p3_v05_general_text_mixed_with_primitives() -> None:
    """General text mixed with primitives or invalid numbers receives NO_UNIFORM_TYPE."""
    res1 = infer_column_type(["hello", "1e309"])
    assert res1.inferred_type == "string"
    assert res1.inference_reason == DECISION_NO_UNIFORM_TYPE

    res2 = infer_column_type(["0abc", "hello"])
    assert res2.inferred_type == "string"
    assert res2.inference_reason == DECISION_NO_UNIFORM_TYPE


def test_p3_v05_mixed_temporal_kinds_before_mixed_awareness() -> None:
    """Mixed temporal kinds (date + datetime) takes precedence over mixed awareness."""
    col = ["2024-01-01", "2024-01-01T12:00:00", "2024-01-01T12:00:00Z"]
    res = infer_column_type(col)
    assert res.inferred_type == "string"
    assert res.inference_reason == DECISION_MIXED_TEMPORAL_KINDS

    from itertools import permutations
    for perm in permutations(col):
        perm_res = infer_column_type(list(perm))
        assert perm_res.inference_reason == DECISION_MIXED_TEMPORAL_KINDS


def test_p3_v05_r1_equivalent_underflow_representations() -> None:
    """Mathematically equivalent underflow representations must receive identical FLOAT_UNDERFLOW reason."""
    pos_sci = "1e-400"
    pos_fixed = "0." + "0" * 399 + "1"
    neg_sci = "-1e-400"
    neg_fixed = "-0." + "0" * 399 + "1"

    additional_cases = [
        "1.0e-400",
        "+1e-400",
        "+0." + "0" * 399 + "1",
        "-1.0e-400",
        ".0" + "0" * 398 + "1",
        "-." + "0" * 398 + "1",
    ]

    all_cases = [pos_sci, pos_fixed, neg_sci, neg_fixed] + additional_cases
    for val_str in all_cases:
        res = infer_column_type([val_str])
        assert res.inferred_type == "string", f"Failed inferred_type for {val_str}"
        assert res.inference_reason == DECISION_FLOAT_UNDERFLOW, (
            f"Failed inference_reason for {val_str}: expected {DECISION_FLOAT_UNDERFLOW}, got {res.inference_reason}"
        )
        assert res.typed_values == [val_str], f"Failed typed_values preservation for {val_str}"
        assert res.null_count == 0

    # Ensure equivalent pairs behave identically
    res_pos_sci = infer_column_type([pos_sci])
    res_pos_fixed = infer_column_type([pos_fixed])
    assert res_pos_sci.inferred_type == res_pos_fixed.inferred_type == "string"
    assert res_pos_sci.inference_reason == res_pos_fixed.inference_reason == DECISION_FLOAT_UNDERFLOW

    res_neg_sci = infer_column_type([neg_sci])
    res_neg_fixed = infer_column_type([neg_fixed])
    assert res_neg_sci.inferred_type == res_neg_fixed.inferred_type == "string"
    assert res_neg_sci.inference_reason == res_neg_fixed.inference_reason == DECISION_FLOAT_UNDERFLOW


def test_p3_v05_r2_mixed_recognized_candidate_families() -> None:
    """Columns mixing incompatible recognized candidate families must evaluate to NO_UNIFORM_TYPE."""
    from itertools import permutations

    cases = [
        ["1e309", "2024-01-01"],        # Numeric + Temporal (date)
        ["1e309", "2024-01-01T00:00:00"], # Numeric + Temporal (datetime)
        ["true", "1e309"],              # Boolean + Numeric
        ["true", "2024-01-01"],         # Boolean + Temporal
        ["42", "2024-01-01"],           # Numeric (int) + Temporal
        ["true", "42"],                 # Boolean + Numeric (int)
    ]

    for col in cases:
        for perm in permutations(col):
            perm_list = list(perm)
            res = infer_column_type(perm_list)
            assert res.inferred_type == "string", f"Failed type for {perm_list}"
            assert res.inference_reason == DECISION_NO_UNIFORM_TYPE, (
                f"Failed reason for {perm_list}: expected {DECISION_NO_UNIFORM_TYPE}, got {res.inference_reason}"
            )
            assert res.typed_values == perm_list, f"Failed typed values for {perm_list}"
            assert res.null_count == 0


def test_p3_v05_temporal_edge_cases_and_precedence() -> None:
    """Temporal candidate evidence survives conversion failures; preserves documented precedence."""
    from itertools import permutations

    # 1. Date + Datetime with submicrosecond precision -> MIXED_TEMPORAL_KINDS across all permutations
    date_and_submicro = ["2024-01-01", "2024-01-01T00:00:00.123456001"]
    for perm in permutations(date_and_submicro):
        perm_list = list(perm)
        res = infer_column_type(perm_list)
        assert res.inferred_type == "string"
        assert res.inference_reason == DECISION_MIXED_TEMPORAL_KINDS, (
            f"Failed on permutation {perm_list}: expected {DECISION_MIXED_TEMPORAL_KINDS}, got {res.inference_reason}"
        )
        assert res.typed_values == perm_list
        assert res.null_count == 0

    # 2. Date + standard Datetime -> MIXED_TEMPORAL_KINDS
    res_dt = infer_column_type(["2024-01-01", "2024-01-01T00:00:00"])
    assert res_dt.inferred_type == "string"
    assert res_dt.inference_reason == DECISION_MIXED_TEMPORAL_KINDS

    # 3. Naive Datetime + Aware Datetime (no dates) -> MIXED_AWARENESS
    res_aw = infer_column_type(["2024-01-01T00:00:00", "2024-01-01T00:00:00+01:00"])
    assert res_aw.inferred_type == "string"
    assert res_aw.inference_reason == DECISION_MIXED_AWARENESS

    # 4. Datetime with valid microsecond + Datetime with submicrosecond precision -> SUBMICROSECOND_PRECISION
    res_sub = infer_column_type(["2024-01-01T00:00:00.123456", "2024-01-01T00:00:00.123456001"])
    assert res_sub.inferred_type == "string"
    assert res_sub.inference_reason == DECISION_SUBMICROSECOND_PRECISION


def test_p3_v05_decimal_context_independence() -> None:
    """Inference is completely invariant to caller-configured ambient Decimal traps and precision."""
    import decimal
    s = "1e-999999999999999999999999"

    # Context 1: Trap InvalidOperation enabled
    ctx1 = decimal.Context(traps=[decimal.InvalidOperation])
    with decimal.localcontext(ctx1):
        res1 = infer_column_type([s])

    # Context 2: Trap disabled, precision low
    ctx2 = decimal.Context(prec=2, traps=[])
    with decimal.localcontext(ctx2):
        res2 = infer_column_type([s])

    assert res1.inferred_type == res2.inferred_type == "string"
    assert res1.typed_values == res2.typed_values == [s]
    assert res1.inference_reason == res2.inference_reason == DECISION_FLOAT_UNDERFLOW


def test_p3_v05_r1_u01_unicode_underflow_equivalence() -> None:
    """ASCII and Unicode decimal digit representations of underflowing values receive identical FLOAT_UNDERFLOW."""
    ascii_sci = "1e-400"
    arabic_sci = "\u0661e-400"  # ١e-400
    fullwidth_sci = "\uff11e-400"  # １e-400

    ascii_dec_exp = "0.1e-399"
    arabic_dec_exp = "0.\u0661e-399"  # 0.١e-399

    pos_fixed_arabic = "0." + "0" * 399 + "\u0661"
    neg_fixed_arabic = "-0." + "0" * 399 + "\u0661"

    cases = [
        # Candidate, expected reason
        (ascii_sci, DECISION_FLOAT_UNDERFLOW),
        (arabic_sci, DECISION_FLOAT_UNDERFLOW),
        (fullwidth_sci, DECISION_FLOAT_UNDERFLOW),
        (ascii_dec_exp, DECISION_FLOAT_UNDERFLOW),
        (arabic_dec_exp, DECISION_FLOAT_UNDERFLOW),
        (pos_fixed_arabic, DECISION_FLOAT_UNDERFLOW),
        (neg_fixed_arabic, DECISION_FLOAT_UNDERFLOW),
        ("+1e-400", DECISION_FLOAT_UNDERFLOW),
        ("+\u0661e-400", DECISION_FLOAT_UNDERFLOW),
        ("+0." + "0" * 399 + "\u0661", DECISION_FLOAT_UNDERFLOW),
        ("1.0e-400", DECISION_FLOAT_UNDERFLOW),
        ("\u0661.0e-400", DECISION_FLOAT_UNDERFLOW),
        ("\u0661.\u0660e-400", DECISION_FLOAT_UNDERFLOW),
        ("-\u0661e-400", DECISION_FLOAT_UNDERFLOW),
        ("-\uff11e-400", DECISION_FLOAT_UNDERFLOW),
    ]

    for val_str, exp_reason in cases:
        res = infer_column_type([val_str])
        assert res.inferred_type == "string", f"Failed inferred_type for {ascii(val_str)}"
        assert res.inference_reason == exp_reason, (
            f"Failed reason for {ascii(val_str)}: expected {exp_reason}, got {res.inference_reason}"
        )
        assert res.typed_values == [val_str], f"Failed typed_values for {ascii(val_str)}"
        assert res.null_count == 0

    # Explicit pairwise equivalence assertions
    res_ascii = infer_column_type([ascii_sci])
    res_arabic = infer_column_type([arabic_sci])
    res_fullwidth = infer_column_type([fullwidth_sci])
    assert res_ascii.inferred_type == res_arabic.inferred_type == res_fullwidth.inferred_type == "string"
    assert res_ascii.inference_reason == res_arabic.inference_reason == res_fullwidth.inference_reason == DECISION_FLOAT_UNDERFLOW

    res_dec_ascii = infer_column_type([ascii_dec_exp])
    res_dec_arabic = infer_column_type([arabic_dec_exp])
    assert res_dec_ascii.inferred_type == res_dec_arabic.inferred_type == "string"
    assert res_dec_ascii.inference_reason == res_dec_arabic.inference_reason == DECISION_FLOAT_UNDERFLOW


def test_p3_v05_r1_u01_zero_handling_non_underflow() -> None:
    """Zero representations (ASCII and Unicode) do not falsely produce FLOAT_UNDERFLOW."""
    zero_float_cases = [
        "0.0",
        "-0.0",
        "0.0e-400",
        "\u0660.0e-400",  # ٠.0e-400
        "\u0660.0",      # ٠.0
        "0.\u0660",      # 0.٠
    ]
    for z in zero_float_cases:
        res = infer_column_type([z])
        assert res.inference_reason != DECISION_FLOAT_UNDERFLOW, (
            f"Zero case {ascii(z)} falsely produced FLOAT_UNDERFLOW"
        )
        assert res.inferred_type == "float", f"Failed inferred_type for zero case {ascii(z)}"
        assert res.inference_reason == DECISION_FLOAT, f"Failed reason for zero case {ascii(z)}"

    # Both ASCII 0e-400 and Unicode ٠e-400 have significant leading zeros (followed by exponent, not decimal point)
    for lead_z in ["0e-400", "\u0660e-400"]:
        res_lead = infer_column_type([lead_z])
        assert res_lead.inference_reason != DECISION_FLOAT_UNDERFLOW
        assert res_lead.inferred_type == "string"
        assert res_lead.inference_reason == DECISION_LEADING_ZERO
        assert res_lead.typed_values == [lead_z]


def test_p3_v05_r1_u01_mixed_inputs_permutations_and_preservation() -> None:
    """Mixed columns with Unicode underflow candidates, nulls, and row permutations."""
    from itertools import permutations

    # 1. Null tokens combined with Unicode underflow candidates
    null_col = ["null", "\u0661e-400", ""]
    res_null = infer_column_type(null_col)
    assert res_null.inferred_type == "string"
    assert res_null.inference_reason == DECISION_FLOAT_UNDERFLOW
    assert res_null.null_count == 2
    assert res_null.typed_values == [None, "\u0661e-400", None]

    # Permutations of null_col preserve row alignment and decision reason
    for perm in permutations(null_col):
        perm_list = list(perm)
        res_perm = infer_column_type(perm_list)
        assert res_perm.inferred_type == "string"
        assert res_perm.inference_reason == DECISION_FLOAT_UNDERFLOW
        assert res_perm.null_count == 2
        for idx, orig in enumerate(perm_list):
            if orig in ("null", ""):
                assert res_perm.typed_values[idx] is None
            else:
                assert res_perm.typed_values[idx] == orig

    # 2. Unicode and ASCII equivalents in the same column
    mixed_col = ["1e-400", "\u0661e-400", "0." + "0" * 399 + "\u0661"]
    for perm in permutations(mixed_col):
        perm_list = list(perm)
        res_mix = infer_column_type(perm_list)
        assert res_mix.inferred_type == "string"
        assert res_mix.inference_reason == DECISION_FLOAT_UNDERFLOW
        assert res_mix.null_count == 0
        assert res_mix.typed_values == perm_list

    # 3. Malformed numeric strings containing Unicode digits fall back to NO_UNIFORM_TYPE
    malformed_cases = [
        ["abc\u0661e-400"],
        ["\u0661e-400-foo"],
        ["\u0661.\u0662.\u0663"],
    ]
    for malformed in malformed_cases:
        res_mal = infer_column_type(malformed)
        assert res_mal.inferred_type == "string"
        assert res_mal.inference_reason == DECISION_NO_UNIFORM_TYPE
        assert res_mal.typed_values == malformed


def test_p3_u02_unicode_leading_zeros_preservation() -> None:
    """Unicode decimal leading zeros are detected across supported repertoire and preserved as LEADING_ZERO."""
    cases = [
        ("0123", "0123"),
        ("\u0660\u0661\u0662\u0663", "\u0660\u0661\u0662\u0663"),  # ٠١٢٣
        ("\uff10\uff11\uff12\uff13", "\uff10\uff11\uff12\uff13"),  # ０１２３
        ("+\u0660\u0661\u0662\u0663", "+\u0660\u0661\u0662\u0663"),  # +٠١٢٣
        ("\u06601.2", "\u06601.2"),  # ٠1.2
        ("007", "007"),
        ("+012", "+012"),
    ]
    for val_str, expected_str in cases:
        assert has_significant_leading_zero(val_str) is True, f"Expected {ascii(val_str)} to have leading zero"
        assert parse_integer(val_str) is None, f"Expected {ascii(val_str)} to be rejected by parse_integer"
        assert parse_float(val_str) is None, f"Expected {ascii(val_str)} to be rejected by parse_float"
        res = infer_column_type([val_str])
        assert res.inferred_type == "string", f"Failed type for {ascii(val_str)}"
        assert res.inference_reason == DECISION_LEADING_ZERO, f"Failed reason for {ascii(val_str)}"
        assert res.typed_values == [expected_str], f"Failed value preservation for {ascii(val_str)}"


def test_p3_u03_unicode_fractional_zero_temporal_rejection() -> None:
    """Unsupported Unicode fractional digits in datetimes are rejected with NO_UNIFORM_TYPE without false temporal evidence."""
    s = "2024-01-01T00:00:00.123456\u0660"  # Unicode zero at the end of fractional second

    # Native parse_datetime rejects unsupported Unicode digits
    assert parse_datetime(s) is None

    # Single column falls back to NO_UNIFORM_TYPE (not SUBMICROSECOND_PRECISION)
    res_single = infer_column_type([s])
    assert res_single.inferred_type == "string"
    assert res_single.inference_reason == DECISION_NO_UNIFORM_TYPE
    assert res_single.typed_values == [s]

    # Mixed with valid date in either order falls back to NO_UNIFORM_TYPE (not MIXED_TEMPORAL_KINDS)
    res_mix1 = infer_column_type(["2024-01-01", s])
    assert res_mix1.inferred_type == "string"
    assert res_mix1.inference_reason == DECISION_NO_UNIFORM_TYPE

    res_mix2 = infer_column_type([s, "2024-01-01"])
    assert res_mix2.inferred_type == "string"
    assert res_mix2.inference_reason == DECISION_NO_UNIFORM_TYPE

    # In contrast, valid ASCII submicrosecond datetime properly establishes temporal evidence
    s_valid = "2024-01-01T00:00:00.123456001"
    assert infer_column_type([s_valid]).inference_reason == DECISION_SUBMICROSECOND_PRECISION
    assert infer_column_type(["2024-01-01", s_valid]).inference_reason == DECISION_MIXED_TEMPORAL_KINDS
    assert infer_column_type([s_valid, "2024-01-01"]).inference_reason == DECISION_MIXED_TEMPORAL_KINDS


