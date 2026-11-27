"""
Data Intelligence Lab - Deterministic insights.

Turns a dataset's profile and (possibly sampled) column values into short findings
written in plain language. Every finding comes from a fixed rule with a fixed
threshold, so the same data always yields the same insights. Nothing is sent
anywhere and no model is involved.

Each insight carries:
  code       stable identifier of the rule (for example "MISSING_VALUES")
  severity   "high", "medium" or "info"
  title      one-line finding
  detail     why it matters / what to check
  columns    column names involved
  evidence   the numbers behind the finding

Wording rules: relationships are described as "move together" or "differ", never as
causes; every relationship rule needs a minimum amount of data before it speaks.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import math
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

METHOD_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Thresholds (documented in docs/INSIGHTS.md; changing one changes METHOD_VERSION)
# ---------------------------------------------------------------------------
MISSING_INFO = 0.05          # report a column from 5% missing
MISSING_MEDIUM = 0.20
MISSING_HIGH = 0.50
ID_MIN_ROWS = 10             # an all-unique column is called an identifier from 10 rows
OUTLIER_MIN_VALUES = 8       # Tukey screening needs at least 8 values
OUTLIER_MEDIUM_SHARE = 0.01  # outliers are "medium" when at least 1% of values
OUTLIER_GROUP_SHARE = 0.10   # above 10% the "outliers" are a second group or long tail, not errors
SKEW_MIN_VALUES = 20
SKEW_MIN_DISTINCT = 10       # ratings and small counts are too coarse for a skew statement
SKEW_THRESHOLD = 1.0         # sample (Fisher-Pearson) skewness g1
CATEGORY_MAX_DISTINCT = 50   # a text column is treated as categorical up to 50 values
DOMINANT_SHARE = 0.80
DATE_MIN_DISTINCT = 7        # date-gap rule needs at least 7 distinct dates
DATE_GAP_MIN_DAYS = 7
DATE_GAP_FACTOR = 5          # a gap is reported when > 5x the typical step
CORR_MIN_PAIRS = 10
CORR_THRESHOLD = 0.70
CORR_MAX_COLUMNS = 25        # at most 25 numeric columns are compared pairwise (300 pairs)
CORR_MAX_RESULTS = 5
GROUP_MAX_CATEGORIES = 20
GROUP_MIN_PER_CATEGORY = 5
GROUP_MIN_RATIO = 2.0        # report when both median and mean of one group are >= 2x another's
GROUP_MAX_PAIRS = 100        # categorical x numeric combinations examined
GROUP_MAX_RESULTS = 3
MAX_COLUMNS_SCANNED = 100    # value-based rules look at the first 100 columns
MAX_INSIGHTS = 25
TIME_BUDGET_SECONDS = 10.0
SAMPLE_ROWS = 20_000         # value-based rules read at most 20,000 evenly spaced rows

_SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}
_NUMERIC = ("integer", "float")


class _Budget:
    """Stops the expensive rules once the time budget is spent (deterministic order)."""

    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._deadline = clock() + seconds
        self.exhausted = False

    def spent(self) -> bool:
        if not self.exhausted and self._clock() > self._deadline:
            self.exhausted = True
        return self.exhausted


def fmt_number(value: float | int) -> str:
    """Readable number: thousands separators, at most 4 significant digits for fractions."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 1e15):
        return f"{int(value):,}"
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.1f}"
    if magnitude >= 1:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:.4g}"


def fmt_pct(share: float) -> str:
    pct = share * 100
    if pct >= 10 or pct == 0:
        return f"{pct:.0f}%"
    if pct >= 1:
        return f"{pct:.1f}%"
    return f"{pct:.2f}%"


def _insight(code: str, severity: str, title: str, detail: str, columns: Sequence[str],
             evidence: Mapping[str, Any], rank: float = 0.0) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "title": title, "detail": detail,
            "columns": list(columns), "evidence": dict(evidence), "_rank": rank}


def _finite_numbers(values: Iterable[Any]) -> List[float]:
    out: List[float] = []
    for v in values:
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            out.append(float(v))
    return out


def _quantile(sorted_values: Sequence[float], p: float) -> float:
    """Linear interpolation at p*(n-1), the convention used by the profile statistics."""
    n = len(sorted_values)
    pos = p * (n - 1)
    low = math.floor(pos)
    high = min(low + 1, n - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


# ---------------------------------------------------------------------------
# Profile-only rules (cheap, always run)
# ---------------------------------------------------------------------------

def _rule_duplicates(row_count: int, duplicate_rows: int) -> List[Dict[str, Any]]:
    if row_count <= 1 or duplicate_rows <= 0:
        return []
    share = duplicate_rows / row_count
    severity = "high" if share >= 0.10 else "medium"
    return [_insight(
        "DUPLICATE_ROWS", severity,
        f"{fmt_number(duplicate_rows)} rows are exact duplicates ({fmt_pct(share)} of the data).",
        "Duplicate rows count twice in every total and average. Check whether the export repeated rows, "
        "and remove the copies if each row should be unique.",
        [], {"duplicate_rows": duplicate_rows, "row_count": row_count, "share": round(share, 4)},
        rank=share)]


def _rule_missing(col: Mapping[str, Any]) -> List[Dict[str, Any]]:
    ratio = float(col.get("null_ratio") or 0.0)
    if ratio < MISSING_INFO or int(col.get("total_count") or 0) == 0:
        return []
    name = col["name"]
    nulls = int(col.get("null_count") or 0)
    if ratio >= 1.0:
        return [_insight(
            "EMPTY_COLUMN", "high", f"'{name}' is completely empty.",
            "The column has no values at all. It can usually be removed, or the export may have lost it.",
            [name], {"null_count": nulls, "null_ratio": ratio}, rank=ratio)]
    severity = "high" if ratio >= MISSING_HIGH else "medium" if ratio >= MISSING_MEDIUM else "info"
    detail = ("Most of this column is missing, so any summary of it describes only a minority of rows."
              if ratio >= MISSING_HIGH else
              "Averages and counts for this column ignore the missing rows. Check whether the gaps are random "
              "or concentrated in one part of the data.")
    return [_insight(
        "MISSING_VALUES", severity,
        f"'{name}' is missing {fmt_pct(ratio)} of its values ({fmt_number(nulls)} rows).",
        detail, [name], {"null_count": nulls, "null_ratio": ratio}, rank=ratio)]


def _rule_constant(col: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if int(col.get("distinct_count") or 0) != 1 or int(col.get("total_count") or 0) <= 1:
        return []
    name = col["name"]
    return [_insight(
        "CONSTANT_COLUMN", "medium", f"'{name}' has the same value in every filled row.",
        "A column with a single value carries no information for analysis. It may be a default or a leftover "
        "from the export.", [name], {"distinct_count": 1})]


def _rule_identifier(col: Mapping[str, Any], row_count: int) -> List[Dict[str, Any]]:
    if not col.get("is_unique") or row_count < ID_MIN_ROWS or col.get("inferred_type") not in ("integer", "string"):
        return []
    name = col["name"]
    return [_insight(
        "IDENTIFIER_COLUMN", "info", f"'{name}' is unique in every row; it looks like an identifier.",
        "Identifiers label rows rather than measure anything, so their averages or totals are not meaningful. "
        "Use them to look up or join rows instead.", [name], {"distinct_count": int(col.get("distinct_count") or 0)})]


def _rule_date_range(col: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if col.get("inferred_type") not in ("date", "datetime"):
        return []
    stats = col.get("stats") or {}
    start, end = stats.get("min_date"), stats.get("max_date")
    if not start or not end:
        return []
    name = col["name"]
    if start == end:
        title = f"'{name}' contains a single {'date' if col.get('inferred_type') == 'date' else 'timestamp'}: {start}."
    else:
        title = f"'{name}' runs from {start} to {end}."
    return [_insight("DATE_RANGE", "info", title,
                     "This is the period the data covers. Compare it with the period you expected.",
                     [name], {"min": start, "max": end})]


# ---------------------------------------------------------------------------
# Value-based rules (use the sampled values)
# ---------------------------------------------------------------------------

def _rule_outliers(name: str, values: Sequence[Any]) -> List[Dict[str, Any]]:
    numbers = sorted(_finite_numbers(values))
    n = len(numbers)
    if n < OUTLIER_MIN_VALUES or numbers[0] == numbers[-1]:
        return []
    q1, q3 = _quantile(numbers, 0.25), _quantile(numbers, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return []
    low_fence, high_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    below = sum(1 for v in numbers if v < low_fence)
    above = sum(1 for v in numbers if v > high_fence)
    count = below + above
    if count == 0:
        return []
    share = count / n
    severity = "medium" if share >= OUTLIER_MEDIUM_SHARE else "info"
    if above and below:
        where = "unusually high and low values"
        bound = f"values below {fmt_number(low_fence)} or above {fmt_number(high_fence)}"
    elif above:
        where = "unusually high values"
        bound = f"values above {fmt_number(high_fence)}"
    else:
        where = "unusually low values"
        bound = f"values below {fmt_number(low_fence)}"
    extreme = numbers[-1] if above else numbers[0]
    inliers = numbers[below:n - above]
    evidence = {"below_fence": below, "above_fence": above, "values_screened": n,
                "lower_fence": low_fence, "upper_fence": high_fence, "most_extreme": extreme}
    if share > OUTLIER_GROUP_SHARE:
        side = "high" if above >= below else "low"
        return [_insight(
            "SEPARATE_VALUE_GROUP", "info",
            f"'{name}' has a long tail or a separate group of {side} values: {fmt_pct(share)} of values "
            f"({fmt_number(count)} of {fmt_number(n)} examined) lie far from the middle half, up to {fmt_number(extreme)}.",
            f"Most values lie between {fmt_number(inliers[0])} and {fmt_number(inliers[-1])}. When this many values "
            "are far out, the column often mixes different kinds of rows (for example two price levels or units). "
            "Compare it across categories before relying on one average.",
            [name], evidence, rank=share)]
    return [_insight(
        "OUTLIERS", severity,
        f"'{name}' has {where}: {fmt_pct(share)} of values ({fmt_number(count)} of {fmt_number(n)} examined) "
        f"fall outside the typical range; the most extreme is {fmt_number(extreme)}.",
        f"The other values lie between {fmt_number(inliers[0])} and {fmt_number(inliers[-1])}; {bound} "
        "(1.5 x the interquartile range beyond the middle half) count as unusual. Check whether the extremes "
        "are real or entry errors before using totals and averages.",
        [name], evidence, rank=share)]


def _rule_skew(name: str, values: Sequence[Any]) -> List[Dict[str, Any]]:
    numbers = _finite_numbers(values)
    n = len(numbers)
    if n < SKEW_MIN_VALUES or len(set(numbers)) < SKEW_MIN_DISTINCT:
        return []
    mean = math.fsum(numbers) / n
    m2 = math.fsum((v - mean) ** 2 for v in numbers) / n
    if m2 <= 0:
        return []
    m3 = math.fsum((v - mean) ** 3 for v in numbers) / n
    g1 = m3 / m2 ** 1.5
    if abs(g1) < SKEW_THRESHOLD:
        return []
    median = _quantile(sorted(numbers), 0.5)
    high = g1 > 0
    return [_insight(
        "SKEWED_DISTRIBUTION", "info",
        f"'{name}' is skewed: a few {'large' if high else 'small'} values pull the average "
        f"{'up' if high else 'down'} (mean {fmt_number(mean)}, median {fmt_number(median)}).",
        "For skewed data the median describes a typical row better than the mean.",
        [name], {"mean": mean, "median": median, "skewness": round(g1, 3), "values_examined": n},
        rank=abs(g1))]


def _rule_dominant_category(name: str, values: Sequence[Any]) -> List[Dict[str, Any]]:
    present = [v for v in values if v is not None and v != ""]
    if len(present) < 10:
        return []
    counts = Counter(present)
    if not 2 <= len(counts) <= CATEGORY_MAX_DISTINCT:
        return []
    top, top_count = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    share = top_count / len(present)
    if share < DOMINANT_SHARE:
        return []
    label = str(top)[:60]
    return [_insight(
        "DOMINANT_CATEGORY", "info",
        f"'{name}' is mostly one value: '{label}' makes up {fmt_pct(share)} of filled rows.",
        "Comparisons across this column rest on very few rows in the other categories.",
        [name], {"top_value": label, "top_share": round(share, 4), "categories": len(counts)}, rank=share)]


def _as_day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _rule_date_gaps(name: str, values: Sequence[Any]) -> List[Dict[str, Any]]:
    days = sorted({d for d in (_as_day(v) for v in values) if d is not None})
    if len(days) < DATE_MIN_DISTINCT:
        return []
    steps = [(b - a).days for a, b in zip(days, days[1:])]
    typical = sorted(steps)[len(steps) // 2]
    largest = max(steps)
    if largest < DATE_GAP_MIN_DAYS or largest <= DATE_GAP_FACTOR * max(typical, 1):
        return []
    i = steps.index(largest)
    start, end = days[i], days[i + 1]
    return [_insight(
        "DATE_GAP", "medium",
        f"'{name}' has a gap of {largest} days with no rows, between {start.isoformat()} and {end.isoformat()}.",
        f"Rows are usually {typical} day{'s' if typical != 1 else ''} apart. Check whether data for this period is "
        "missing from the export.", [name],
        {"gap_days": largest, "gap_start": start.isoformat(), "gap_end": end.isoformat(), "typical_step_days": typical},
        rank=largest / max(typical, 1))]


def _rule_correlations(numeric: Sequence[str], values: Mapping[str, Sequence[Any]],
                       budget: _Budget) -> List[Dict[str, Any]]:
    cols = list(numeric)[:CORR_MAX_COLUMNS]
    found: List[Tuple[float, str, str, float, int]] = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            if budget.spent():
                break
            xs: List[float] = []
            ys: List[float] = []
            for x, y in zip(values[a], values[b]):
                if (isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                        and isinstance(y, (int, float)) and not isinstance(y, bool) and math.isfinite(y)):
                    xs.append(float(x))
                    ys.append(float(y))
            if len(xs) < CORR_MIN_PAIRS:
                continue
            r = _pearson(xs, ys)
            if r is not None and abs(r) >= CORR_THRESHOLD:
                found.append((abs(r), a, b, r, len(xs)))
    found.sort(key=lambda t: (-t[0], t[1], t[2]))
    out = []
    for strength, a, b, r, n in found[:CORR_MAX_RESULTS]:
        direction = "rise together" if r > 0 else "move in opposite directions"
        out.append(_insight(
            "CORRELATION", "info",
            f"'{a}' and '{b}' {direction} (correlation {r:+.2f}).",
            "A strong correlation means the two columns are related in this data. It does not show that one causes "
            "the other; both may depend on something else.", [a, b],
            {"pearson_r": round(r, 4), "pairs": n}, rank=strength))
    return out


def _rule_group_differences(categorical: Sequence[str], numeric: Sequence[str],
                            values: Mapping[str, Sequence[Any]], budget: _Budget) -> List[Dict[str, Any]]:
    found = []
    examined = 0
    for cat in categorical:
        for num in numeric:
            if examined >= GROUP_MAX_PAIRS or budget.spent():
                break
            examined += 1
            groups: Dict[Any, List[float]] = {}
            for c, v in zip(values[cat], values[num]):
                if c is None or c == "" or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    continue
                groups.setdefault(c, []).append(float(v))
            groups = {k: g for k, g in groups.items() if len(g) >= GROUP_MIN_PER_CATEGORY}
            if not 2 <= len(groups) <= GROUP_MAX_CATEGORIES:
                continue
            # The median and the mean must both differ: medians stop one extreme row from deciding,
            # means stop a few discrete values (0, 5, 10...) from flipping a median by chance.
            medians = {k: _quantile(sorted(g), 0.5) for k, g in groups.items()}
            means = {k: math.fsum(g) / len(g) for k, g in groups.items()}
            if min(medians.values()) <= 0 or min(means.values()) <= 0:
                continue  # ratios are only meaningful for positive quantities
            hi = max(medians, key=lambda k: (medians[k], str(k)))
            lo = min(medians, key=lambda k: (medians[k], str(k)))
            ratio = medians[hi] / medians[lo]
            if ratio >= GROUP_MIN_RATIO and means[hi] / means[lo] >= GROUP_MIN_RATIO:
                found.append((ratio, cat, num, hi, lo, medians[hi], medians[lo], len(groups[hi]), len(groups[lo])))
    found.sort(key=lambda t: (-t[0], t[1], t[2]))
    out = []
    for ratio, cat, num, hi, lo, mhi, mlo, nhi, nlo in found[:GROUP_MAX_RESULTS]:
        out.append(_insight(
            "GROUP_DIFFERENCE", "info",
            f"Typical '{num}' is {ratio:.1f}x higher for '{str(hi)[:40]}' (median {fmt_number(mhi)}) than for "
            f"'{str(lo)[:40]}' (median {fmt_number(mlo)}) in '{cat}'.",
            f"Based on {fmt_number(nhi)} and {fmt_number(nlo)} rows. This is a difference between groups in this "
            "data, not proof of a cause.", [cat, num],
            {"highest_group": str(hi)[:60], "highest_median": mhi, "highest_rows": nhi,
             "lowest_group": str(lo)[:60], "lowest_median": mlo, "lowest_rows": nlo, "ratio": round(ratio, 3)},
            rank=ratio))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def sample_indices(row_count: int, limit: int = SAMPLE_ROWS) -> List[int]:
    """Evenly spaced row positions (always the same for the same row count)."""
    if row_count <= limit:
        return list(range(row_count))
    step = row_count / limit
    return [int(i * step) for i in range(limit)]


def generate_insights(
    columns: Sequence[Mapping[str, Any]],
    values: Mapping[str, Sequence[Any]],
    row_count: int,
    duplicate_rows: int,
    rows_examined: Optional[int] = None,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """
    Builds the insight list for a dataset.

    columns: the serialized column profiles (name, inferred_type, total_count, null_count,
             null_ratio, distinct_count, is_unique, stats), as stored in metadata.json.
    values:  column name -> list of typed values for the rows examined (all columns aligned
             row by row). Columns absent from `values` are skipped by the value-based rules.
    rows_examined: number of rows in `values` (fewer than row_count when sampled).
    """
    budget = _Budget(time_budget_seconds, clock)
    items: List[Dict[str, Any]] = []
    items += _rule_duplicates(row_count, duplicate_rows)
    for col in columns:
        items += _rule_missing(col)
        items += _rule_constant(col)
        items += _rule_identifier(col, row_count)
        items += _rule_date_range(col)

    scanned = [c for c in columns if c.get("name") in values][:MAX_COLUMNS_SCANNED]
    numeric = [c["name"] for c in scanned if c.get("inferred_type") in _NUMERIC and not c.get("is_unique")]
    categorical = [c["name"] for c in scanned
                   if c.get("inferred_type") in ("string", "boolean")
                   and 2 <= int(c.get("distinct_count") or 0) <= CATEGORY_MAX_DISTINCT]
    for c in scanned:
        if budget.spent():
            break
        name = c["name"]
        if c.get("inferred_type") in _NUMERIC and not c.get("is_unique"):
            items += _rule_outliers(name, values[name])
            items += _rule_skew(name, values[name])
        elif c.get("inferred_type") == "string":
            items += _rule_dominant_category(name, values[name])
        elif c.get("inferred_type") in ("date", "datetime"):
            items += _rule_date_gaps(name, values[name])
    if not budget.spent():
        items += _rule_correlations(numeric, values, budget)
    if not budget.spent():
        items += _rule_group_differences(categorical, numeric, values, budget)

    items.sort(key=lambda it: (_SEVERITY_ORDER[it["severity"]], -it["_rank"], it["code"], it["columns"]))
    truncated = len(items) > MAX_INSIGHTS
    items = items[:MAX_INSIGHTS]
    for it in items:
        it.pop("_rank", None)
    examined = row_count if rows_examined is None else rows_examined
    return {
        "method_version": METHOD_VERSION,
        "rows_examined": examined,
        "sampled": examined < row_count,
        "partial": budget.exhausted,
        "truncated": truncated,
        "items": items,
    }
