"""Doctype inference from source headers and file name.

Lets `map`/`import` (and the orchestrator agent) run a file without an explicit
`--doctype`. Two signals, strongest first:

1. Header identity columns (content — the most reliable signal).
2. File-name prefix (naming convention: customers*.csv, items*.csv,
   addresses*.csv, contacts*.csv).

Flat customers_full sheets (Customer Name + inline contact/address columns) have
no single doctype and are reported as None regardless of file name — callers
route them to the customers_full flow instead.
"""
from __future__ import annotations

from typing import Optional

from .customers_full import is_customers_full_sheet
from .source import SourceTable

# Strong identity columns per doctype (checked case-insensitively).
_PRIMARY_HEADERS = [
    ("Customer", ["Customer Name"]),
    ("Supplier", ["Supplier Name"]),
    ("Item", ["Item Code"]),
    ("Contact", ["First Name", "Contact Name"]),
    ("Address", ["Address Title", "Address Line 1"]),
]

# File-name prefix convention (checked case-insensitively against the basename).
_FILE_PREFIXES = [
    ("customers", "Customer"),
    ("items", "Item"),
    ("addresses", "Address"),
    ("contacts", "Contact"),
]


def guess_doctype(source: SourceTable) -> Optional[str]:
    """Return the doctype a source maps to, or None (flat customers_full sheet / unknown)."""
    if is_customers_full_sheet(source):
        return None

    # 1) header identity column (content beats naming)
    headers = {h.strip().lower() for h in source.headers}
    for doctype, primaries in _PRIMARY_HEADERS:
        if any(p.lower() in headers for p in primaries):
            return doctype

    # 2) file-name prefix convention
    basename = source.name.lower()
    for prefix, doctype in _FILE_PREFIXES:
        if basename.startswith(prefix):
            return doctype

    return None
