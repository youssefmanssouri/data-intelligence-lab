"""
Data Intelligence Lab - Header and Cell Sanitization
Sanitizes untrusted CSV headers, prevents CSV formula injection, and normalizes identifiers.
"""

from __future__ import annotations
import re
from typing import List, Set, Tuple

# Spreadsheet formula trigger characters (CWE-1236: CSV Injection)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_header_value(raw: str) -> str:
    """
    Sanitizes raw header string by trimming whitespace and neutralizing formula injection characters.
    """
    cleaned = raw.strip()
    # If header starts with a formula character, strip leading occurrences
    while cleaned and cleaned.startswith(FORMULA_PREFIXES):
        cleaned = cleaned.lstrip("=+-@\t\r ").strip()
    return cleaned


def to_snake_case_identifier(text: str) -> str:
    """
    Converts arbitrary header string to a safe, clean snake_case identifier [a-z0-9_].
    Examples:
        'Customer ID' -> 'customer_id'
        'Revenue ($)' -> 'revenue'
        '2024 Total' -> 'total_2024' or 'col_2024_total'
    """
    # Replace non-alphanumeric characters with underscores
    s = re.sub(r"[^\w\s-]", "", text)
    s = re.sub(r"[-\s]+", "_", s).strip("_").lower()
    
    # If result starts with a digit, prefix with 'c_' so it is a valid SQL/Python identifier
    if s and s[0].isdigit():
        s = f"c_{s}"
    return s


def normalize_headers(raw_headers: List[str]) -> List[Tuple[str, str]]:
    """
    Normalizes a list of untrusted headers into safe, unique, snake_case column names.
    Returns a list of tuples: (normalized_name, sanitized_raw_name).
    
    Guarantees:
    1. Empty headers are replaced with 'unnamed_1', 'unnamed_2', etc.
    2. Duplicate headers are deterministically deduplicated (e.g., 'id', 'id_1', 'id_2').
    3. Formula injection prefixes are stripped.
    4. Original header values are preserved for display and traceability.
    """
    seen: Set[str] = set()
    result: List[Tuple[str, str]] = []

    for idx, raw in enumerate(raw_headers):
        sanitized_raw = sanitize_header_value(raw)
        base_name = to_snake_case_identifier(sanitized_raw)
        
        # Fallback for empty or all-special-character headers
        if not base_name:
            base_name = f"unnamed_{idx + 1}"

        # Ensure uniqueness across the dataset
        candidate_name = base_name
        counter = 1
        while candidate_name in seen:
            candidate_name = f"{base_name}_{counter}"
            counter += 1

        seen.add(candidate_name)
        result.append((candidate_name, sanitized_raw if sanitized_raw else f"Unnamed Column {idx + 1}"))

    return result
