"""
Data Intelligence Lab - Type Inference
Infers primitive data types safely without silent precision loss or accidental conversions.
Enforces conservative numeric policy, strict temporal validation, and deterministic decision tracking.
"""

from __future__ import annotations
from datetime import date, datetime
import decimal
from decimal import Decimal
import math
import re
from typing import Any, Callable, List, Optional, Sequence, Set, Tuple
import unicodedata

from .types import (
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
)

NULL_REPRESENTATIONS: Set[str] = {
    "", "na", "n/a", "null", "none", "nan", "-", "nil", "missing", "#n/a"
}

# ISO-8601 Date regex: YYYY-MM-DD (ASCII digits)
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# ISO-8601 Datetime regex: YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD HH:MM:SS (ASCII digits)
DATETIME_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?$")

# Subsecond extractor to detect >6 decimal fractional seconds (ASCII digits)
DATETIME_SUBSECOND_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}\.([0-9]+)")

# Explicit timezone offset regex: [+-]HH:MM or [+-]HHMM (ASCII digits)
DATETIME_TZ_OFFSET_RE = re.compile(r"([+-])([0-9]{2}):?([0-9]{2})$")

# Integer regex: optional sign followed by digits
INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")

# Float regex: optional sign, digits, decimal point, digits, optional exponent
FLOAT_PATTERN = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")

# Isolated standard Decimal context for type inference (prec=50, no traps)
# Guarantees Decimal operations are 100% immune to caller-controlled ambient Decimal context
INFERENCE_DECIMAL_CONTEXT = decimal.Context(
    prec=50,
    Emax=999999,
    Emin=-999999,
    traps=[],
)


def is_null_value(val: str) -> bool:
    """Checks whether a string cell represents a missing/null value."""
    return val.strip().lower() in NULL_REPRESENTATIONS


def has_nonzero_decimal_digit(s: str) -> bool:
    """
    Checks whether a numeric string or mantissa contains at least one non-zero
    decimal digit accepted by the numeric grammar (including Unicode decimal digits, e.g. '1', '١', '１').
    Uses unicodedata.decimal() to accurately identify decimal digit values.
    """
    for ch in s:
        d = unicodedata.decimal(ch, None)
        if d is not None and d != 0:
            return True
    return False


def has_significant_leading_zero(val: str) -> bool:
    """
    Checks if a number string has leading zeros (e.g. '0123', '007', '+012', '0123.4', '٠١٢٣').
    Only applies to numeric candidates (matching INTEGER_PATTERN or FLOAT_PATTERN),
    never to general text like '0abc'.
    """
    s = val.strip()
    if not (INTEGER_PATTERN.match(s) or FLOAT_PATTERN.match(s)):
        return False
    s_clean = s.lstrip("+-")
    if len(s_clean) <= 1:
        return False
    return unicodedata.decimal(s_clean[0], None) == 0 and s_clean[1] != "."


def parse_boolean(val: str) -> Optional[bool]:
    """Parses strict boolean representations. Does not parse '1'/'0' as boolean."""
    s = val.strip().lower()
    if s in ("true", "t", "yes"):
        return True
    if s in ("false", "f", "no"):
        return False
    return None


def parse_integer(val: str) -> Optional[int]:
    """Parses an integer string if valid and lacks significant leading zeros."""
    s = val.strip()
    if not INTEGER_PATTERN.match(s) or has_significant_leading_zero(s):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_float(val: str) -> Optional[float]:
    """
    Parses a floating-point number with strict IEEE-754 precision checks.
    Rejects:
    - Significant leading zeros (e.g. '0123.4')
    - Non-finite tokens ('inf', '-inf', 'nan', etc.)
    - Nonzero-to-zero underflow (e.g. '1e-400')
    - Decimal precision loss (Decimal(str(f)) != d)
    - Integral value mismatches across all mathematically integral candidates
    Preserves -0.0 when negative zero is parsed.
    """
    s = val.strip()
    if s.lower() in ("inf", "-inf", "+inf", "nan", "infinity", "+infinity", "-infinity"):
        return None
    if not FLOAT_PATTERN.match(s) or has_significant_leading_zero(s):
        return None
    try:
        f = float(s)
    except Exception:
        return None

    if not math.isfinite(f):
        return None

    # Underflow check: non-zero value underflowing to 0.0
    if f == 0.0:
        mantissa = re.split(r"[eE]", s)[0]
        if has_nonzero_decimal_digit(mantissa):
            return None

    # Decimal equality and exact integral checks under isolated Decimal context
    with decimal.localcontext(INFERENCE_DECIMAL_CONTEXT):
        try:
            d = Decimal(s)
            if d.is_nan():
                return None
        except Exception:
            return None

        # Decimal equality check: str(f) must round-trip exactly to Decimal(s)
        try:
            if Decimal(str(f)) != d:
                return None
        except Exception:
            return None

        # Exact integral-value condition: applies to EVERY mathematically integral candidate
        # regardless of whether the representation contains 'e' or 'E'
        try:
            if d == d.to_integral_value():
                if int(f) != int(d):
                    return None
        except Exception:
            return None

    # Preserve -0.0
    if f == 0.0 and s.startswith("-"):
        f = -0.0

    return f


def parse_date(val: str) -> Optional[date]:
    """Parses standard ISO-8601 date string (YYYY-MM-DD). Validates calendar components."""
    s = val.strip()
    if not DATE_PATTERN.match(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_datetime(val: str) -> Optional[datetime]:
    """
    Parses ISO-8601 datetime strings without lossy precision truncation.
    Rejects strings with submicrosecond precision (>6 fractional digits with non-zero info).
    Validates timezone offset components explicitly before datetime.fromisoformat.
    Preserves timezone awareness and exact per-value UTC offsets.
    """
    s = val.strip()
    if not DATETIME_PATTERN.match(s):
        return None

    # Submicrosecond precision check:
    # <= 6 digits -> valid
    # > 6 digits with only trailing zeros -> valid and exactly representable
    # > 6 digits with non-zero submicrosecond info -> reject
    m_sub = DATETIME_SUBSECOND_RE.match(s)
    if m_sub:
        frac = m_sub.group(1)
        if len(frac) > 6 and frac[6:].rstrip("0") != "":
            return None

    # Timezone offset component validation:
    # Explicitly validate hours (00-23) and minutes (00-59) to prevent Python's fromisoformat
    # from silently normalizing invalid offsets like +01:60 -> +02:00 or -00:99 -> -01:39
    m_tz = DATETIME_TZ_OFFSET_RE.search(s)
    if m_tz:
        tz_hour = int(m_tz.group(2))
        tz_min = int(m_tz.group(3))
        if tz_hour > 23 or tz_min > 59:
            return None

    try:
        iso_str = s.replace(" ", "T")
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return None


INFERENCE_DEADLINE_POLL_INTERVAL: int = 500


def infer_column_type(
    raw_values: Sequence[str],
    deadline: Optional[ExecutionDeadline] = None,
    poll_interval: int = INFERENCE_DEADLINE_POLL_INTERVAL,
    *,
    is_literal: bool = False,
) -> InferenceResult:
    """
    Infers the most specific uniform primitive type for a sequence of raw string cell values.

    Type hierarchy (most specific to fallback):
    1. boolean
    2. integer
    3. float
    4. date
    5. datetime
    6. string

    Features:
    - Opt-in literal text preservation: bypasses null parsing, preserving exact strings.
    - Deterministic decision code generation independent of row permutations.
    - Cooperative deadline polling throughout loops.

    Returns:
        InferenceResult: tuple subclass of (inferred_type, typed_values, null_count)
                         with .inference_reason attribute.
    """
    if deadline is not None:
        deadline.check()

    # 0. Opt-in Literal Column Override
    if is_literal:
        typed_lit: List[Any] = []
        for idx, v in enumerate(raw_values):
            if idx % poll_interval == 0 and deadline is not None:
                deadline.check()
            typed_lit.append(v)
        if deadline is not None:
            deadline.check()
        return InferenceResult(
            inferred_type="string",
            typed_values=typed_lit,
            null_count=0,
            inference_reason=DECISION_PRESERVE_AS_TEXT,
        )

    # Fast check: determine if there are any non-null values with periodic deadline polling
    has_non_null = False
    for idx, v in enumerate(raw_values):
        if idx % poll_interval == 0 and deadline is not None:
            deadline.check()
        if not is_null_value(v):
            has_non_null = True
            break

    # All values are null (or empty dataset) -> default to string with all None
    if not has_non_null:
        return InferenceResult(
            inferred_type="string",
            typed_values=[None] * len(raw_values),
            null_count=len(raw_values),
            inference_reason=DECISION_ALL_NULL,
        )

    def matches_all_non_null(parser: Callable[[str], Optional[Any]]) -> bool:
        """Returns True if every non-null cell matches the parser, polling deadline periodically."""
        for idx, v in enumerate(raw_values):
            if idx % poll_interval == 0 and deadline is not None:
                deadline.check()
            if not is_null_value(v):
                if parser(v) is None:
                    return False
        return True

    def convert_column(parser: Callable[[str], Any]) -> Tuple[List[Any], int]:
        """Converts raw string values to typed values and counts nulls with periodic deadline polling."""
        result: List[Any] = []
        null_count = 0
        for idx, v in enumerate(raw_values):
            if idx % poll_interval == 0 and deadline is not None:
                deadline.check()
            if not is_null_value(v):
                result.append(parser(v))
            else:
                result.append(None)
                null_count += 1
        return result, null_count

    # 1. Test Boolean
    if deadline is not None:
        deadline.check()
    if matches_all_non_null(parse_boolean):
        typed, null_count = convert_column(parse_boolean)
        return InferenceResult("boolean", typed, null_count, DECISION_BOOLEAN)

    # 2. Test Integer
    if deadline is not None:
        deadline.check()
    if matches_all_non_null(parse_integer):
        typed, null_count = convert_column(parse_integer)
        return InferenceResult("integer", typed, null_count, DECISION_INTEGER)

    # 3. Test Float
    if deadline is not None:
        deadline.check()
    if matches_all_non_null(parse_float):
        typed, null_count = convert_column(parse_float)
        return InferenceResult("float", typed, null_count, DECISION_FLOAT)

    # 4. Test Date (YYYY-MM-DD)
    if deadline is not None:
        deadline.check()
    if matches_all_non_null(parse_date):
        typed, null_count = convert_column(parse_date)
        return InferenceResult("date", typed, null_count, DECISION_DATE)

    # 5. Test Datetime (Uniform Timezone Awareness)
    if deadline is not None:
        deadline.check()
    if matches_all_non_null(parse_datetime):
        typed, null_count = convert_column(parse_datetime)
        first_aware: Optional[bool] = None
        has_mixed_awareness = False
        for idx, val in enumerate(typed):
            if idx % poll_interval == 0 and deadline is not None:
                deadline.check()
            if val is not None:
                is_aware = (val.tzinfo is not None)
                if first_aware is None:
                    first_aware = is_aware
                elif is_aware != first_aware:
                    has_mixed_awareness = True
                    break
        if not has_mixed_awareness:
            return InferenceResult("datetime", typed, null_count, DECISION_DATETIME)

    # 6. Fallback: String
    if deadline is not None:
        deadline.check()
    typed: List[Any] = []
    null_count = 0
    for idx, v in enumerate(raw_values):
        if idx % poll_interval == 0 and deadline is not None:
            deadline.check()
        if not is_null_value(v):
            # Phase 3: Original non-null strings survive ordinary fallback without .strip()
            typed.append(v)
        else:
            typed.append(None)
            null_count += 1

    # Scan column non-null values to determine deterministic fallback reason code
    has_general_text = False
    has_leading_zero = False
    has_float_precision_loss = False
    has_float_underflow = False
    has_non_finite_float = False
    has_submicrosecond_precision = False
    has_aware_dt = False
    has_naive_dt = False
    has_any_date = False
    has_any_dt = False
    families_present: Set[str] = set()

    for idx, v in enumerate(raw_values):
        if idx % poll_interval == 0 and deadline is not None:
            deadline.check()
        if is_null_value(v):
            continue
        s = v.strip()

        # Candidate family classification
        is_bool_cand = s.lower() in ("true", "t", "yes", "false", "f", "no")
        is_int_cand = bool(INTEGER_PATTERN.match(s))
        is_float_cand = bool(FLOAT_PATTERN.match(s)) or s.lower() in (
            "inf", "-inf", "+inf", "nan", "infinity", "+infinity", "-infinity"
        )
        has_lead_zero = has_significant_leading_zero(s)
        is_date_cand = bool(DATE_PATTERN.match(s))
        is_dt_cand = bool(DATETIME_PATTERN.match(s))

        matched_any_family = False

        # 1. Boolean candidate
        if is_bool_cand:
            families_present.add("boolean")
            matched_any_family = True

        # 2. Numeric candidate (integers, floats, leading zeros, non-finites)
        if is_int_cand or is_float_cand or has_lead_zero:
            families_present.add("numeric")
            matched_any_family = True

            if has_lead_zero:
                has_leading_zero = True

            if s.lower() in ("inf", "-inf", "+inf", "nan", "infinity", "+infinity", "-infinity"):
                has_non_finite_float = True

            if is_float_cand and not has_lead_zero:
                try:
                    f = float(s)
                    if not math.isfinite(f):
                        has_non_finite_float = True
                    elif f == 0.0:
                        mantissa = re.split(r"[eE]", s)[0]
                        if has_nonzero_decimal_digit(mantissa):
                            has_float_underflow = True
                    else:
                        # Check precision loss with Decimal in isolated local context
                        with decimal.localcontext(INFERENCE_DECIMAL_CONTEXT):
                            try:
                                d = Decimal(s)
                                if not d.is_nan():
                                    if Decimal(str(f)) != d:
                                        has_float_precision_loss = True
                                    elif d == d.to_integral_value():
                                        if int(f) != int(d):
                                            has_float_precision_loss = True
                            except Exception:
                                pass
                except Exception:
                    pass

        # 3. Temporal candidate (date or datetime)
        if is_date_cand:
            if parse_date(v) is not None:
                families_present.add("temporal")
                has_any_date = True
                matched_any_family = True
            else:
                has_general_text = True
                families_present.add("text")
                matched_any_family = True

        if is_dt_cand:
            # Check for invalid offset minutes/hours first
            m_tz = DATETIME_TZ_OFFSET_RE.search(s)
            if m_tz and (int(m_tz.group(2)) > 23 or int(m_tz.group(3)) > 59):
                has_general_text = True
                families_present.add("text")
                matched_any_family = True
            else:
                m_sub = DATETIME_SUBSECOND_RE.match(s)
                if m_sub and len(m_sub.group(1)) > 6 and m_sub.group(1)[6:].rstrip("0") != "":
                    # Submicrosecond precision: validate base datetime structure
                    truncated_s = s[:m_sub.start(1) + 6] + s[m_sub.end(1):]
                    parsed_base = parse_datetime(truncated_s)
                    if parsed_base is not None:
                        families_present.add("temporal")
                        has_any_dt = True
                        has_submicrosecond_precision = True
                        matched_any_family = True
                        if parsed_base.tzinfo is not None:
                            has_aware_dt = True
                        else:
                            has_naive_dt = True
                    else:
                        has_general_text = True
                        families_present.add("text")
                        matched_any_family = True
                else:
                    parsed_dt = parse_datetime(v)
                    if parsed_dt is not None:
                        families_present.add("temporal")
                        has_any_dt = True
                        matched_any_family = True
                        if parsed_dt.tzinfo is not None:
                            has_aware_dt = True
                        else:
                            has_naive_dt = True
                    else:
                        has_general_text = True
                        families_present.add("text")
                        matched_any_family = True

        if not matched_any_family:
            has_general_text = True
            families_present.add("text")

    has_mixed_awareness = has_aware_dt and has_naive_dt
    has_mixed_temporal_kinds = has_any_date and has_any_dt

    # Strict Deterministic Precedence Hierarchy
    if has_general_text or len(families_present) != 1:
        fallback_reason = DECISION_NO_UNIFORM_TYPE
    elif "numeric" in families_present:
        if has_leading_zero:
            fallback_reason = DECISION_LEADING_ZERO
        elif has_float_precision_loss:
            fallback_reason = DECISION_FLOAT_PRECISION_LOSS
        elif has_float_underflow:
            fallback_reason = DECISION_FLOAT_UNDERFLOW
        elif has_non_finite_float:
            fallback_reason = DECISION_NON_FINITE_FLOAT
        else:
            fallback_reason = DECISION_NO_UNIFORM_TYPE
    elif "temporal" in families_present:
        if has_mixed_temporal_kinds:
            fallback_reason = DECISION_MIXED_TEMPORAL_KINDS
        elif has_submicrosecond_precision:
            fallback_reason = DECISION_SUBMICROSECOND_PRECISION
        elif has_mixed_awareness:
            fallback_reason = DECISION_MIXED_AWARENESS
        else:
            fallback_reason = DECISION_NO_UNIFORM_TYPE
    else:
        fallback_reason = DECISION_NO_UNIFORM_TYPE

    return InferenceResult("string", typed, null_count, fallback_reason)
