"""
Data Intelligence Lab - Encoding and Delimiter Detection
Provides deterministic sniffing of character encodings and tabular delimiters.
"""

from __future__ import annotations
import csv
import io
from typing import Dict, List, Optional, Sequence, TextIO, Tuple, Union
from .types import DelimiterDetectionError, EncodingDetectionError, ExecutionDeadline

CANDIDATE_DELIMITERS: Tuple[str, ...] = (",", ";", "\t", "|")


def detect_and_decode_encoding(
    raw_bytes: bytes,
    deadline: Optional[ExecutionDeadline] = None,
) -> Tuple[str, str]:
    """
    Coordinates character encoding selection and decoding over bounded complete input.

    Order of precedence:
    1. BOM-directed:
       - UTF-8 BOM (\\xef\\xbb\\xbf): Decoded strictly with 'utf-8-sig'.
       - UTF-16 BOM (\\xff\\xfe or \\xfe\\xff): Decoded strictly with 'utf-16'.
       - If BOM is present but content is invalid, raises EncodingDetectionError (no fallback).
    2. Without BOM:
       - Strict complete-input UTF-8 ('utf-8').
       - Strict complete-input CP1252 ('cp1252').
       - Strict Latin-1 ('latin-1') as final legacy fallback.

    Known Limitation:
        Malformed UTF-8 without a BOM may contain byte sequences that happen to be valid
        under CP1252 or Latin-1; without an explicit BOM or external metadata, such input
        is inherently indistinguishable from legitimate legacy-encoded text.

    Returns:
        (encoding_name, decoded_text)

    Raises:
        EncodingDetectionError: If BOM-declared content is invalid or content cannot be decoded.
    """
    if not raw_bytes:
        return "utf-8", ""

    if deadline is not None:
        deadline.check()

    # 1. Recognized BOMs
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        try:
            return "utf-8-sig", raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise EncodingDetectionError(f"Invalid UTF-8 content with UTF-8 BOM: {e}") from None

    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        try:
            return "utf-16", raw_bytes.decode("utf-16")
        except (UnicodeDecodeError, ValueError) as e:
            raise EncodingDetectionError(f"Invalid UTF-16 content with UTF-16 BOM: {e}") from None

    # 2. No BOM: Strict complete-input UTF-8
    if deadline is not None:
        deadline.check()
    try:
        return "utf-8", raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 3. No BOM: Strict complete-input CP1252
    if deadline is not None:
        deadline.check()
    try:
        return "cp1252", raw_bytes.decode("cp1252")
    except UnicodeDecodeError:
        pass

    # 4. Final legacy fallback: Latin-1
    if deadline is not None:
        deadline.check()
    try:
        return "latin-1", raw_bytes.decode("latin-1")
    except (UnicodeDecodeError, ValueError) as e:
        raise EncodingDetectionError(f"Unsupported character encoding or corrupted binary stream: {e}") from None


def detect_encoding(sample_bytes: bytes) -> str:
    """
    Public wrapper preserving backwards compatibility. Returns detected encoding name.
    Strictly validates BOM-prefixed inputs; invalid content declared by a BOM raises
    EncodingDetectionError rather than silently falling back to Latin-1.
    """
    encoding, _ = detect_and_decode_encoding(sample_bytes)
    return encoding


def detect_delimiter(
    sample_text_or_stream: Union[str, io.StringIO, TextIO] = "",
    candidate_delimiters: Sequence[str] = CANDIDATE_DELIMITERS,
    deadline: Optional[ExecutionDeadline] = None,
    *,
    sample_text: Optional[Union[str, io.StringIO, TextIO]] = None,
) -> str:
    """
    Deterministically detects the column delimiter using logical record CSV parsing.

    Algorithm:
    1. Parse candidate headers using actual CSV parsing semantics (strict=True, quotechar='"', doublequote=True).
    2. Recognize quoted delimiters, doubled quotes, and embedded newlines without stripping meaningful tabs or spaces.
    3. If exactly one candidate produces a viable multi-column header (len > 1), select it immediately.
       (Does not reject an otherwise uniquely identified delimiter merely because a later record is ragged).
    4. If multiple candidates remain plausible, examine up to 10 complete logical records per candidate
       and select only a unique structurally consistent interpretation.
    5. If input remains ambiguous, raise DelimiterDetectionError.
    6. If no viable multi-column interpretation exists, preserve ',' as the single-column convention.
       Does not select an arbitrary alternative delimiter that would mask malformed canonical comma CSV.
    7. Propagates shared deadline checks throughout candidate attempts.

    Raises:
        DelimiterDetectionError: If text is empty or delimiter is ambiguous.
    """
    source = sample_text if sample_text is not None else sample_text_or_stream
    if isinstance(source, str):
        stream = io.StringIO(source, newline="")
    else:
        stream = source

    viable_candidates: Dict[str, List[str]] = {}

    for delim in candidate_delimiters:
        if deadline is not None:
            deadline.check()
        stream.seek(0)
        reader = csv.reader(
            stream,
            delimiter=delim,
            quotechar='"',
            doublequote=True,
            escapechar=None,
            skipinitialspace=False,
            strict=True,
        )
        try:
            header = next(reader)
        except (StopIteration, csv.Error):
            continue
        if len(header) > 1:
            viable_candidates[delim] = header

    if not viable_candidates:
        # Check if stream has any readable content
        stream.seek(0)
        content = stream.read()
        stream.seek(0)
        if not content.strip():
            raise DelimiterDetectionError("Cannot detect delimiter on empty sample text.")
        # Canonical single-column convention is comma; do NOT fall back to arbitrary alternative delimiters
        return ","

    if len(viable_candidates) == 1:
        delim = next(iter(viable_candidates.keys()))
        stream.seek(0)
        return delim

    # Multiple candidates remain plausible: examine up to 10 complete logical records per candidate
    consistent_candidates: List[str] = []
    for delim, header in viable_candidates.items():
        expected_cols = len(header)
        is_consistent = True
        stream.seek(0)
        reader = csv.reader(
            stream,
            delimiter=delim,
            quotechar='"',
            doublequote=True,
            escapechar=None,
            skipinitialspace=False,
            strict=True,
        )
        try:
            next(reader)  # Skip header
        except (StopIteration, csv.Error):
            continue

        for _ in range(9):
            if deadline is not None:
                deadline.check()
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error:
                is_consistent = False
                break
            if len(row) != expected_cols:
                is_consistent = False
                break

        if is_consistent:
            consistent_candidates.append(delim)

    stream.seek(0)
    if len(consistent_candidates) == 1:
        return consistent_candidates[0]

    raise DelimiterDetectionError(
        f"Ambiguous delimiter: multiple candidates {consistent_candidates or list(viable_candidates.keys())} "
        f"remain plausible or inconsistent across sample records."
    )
