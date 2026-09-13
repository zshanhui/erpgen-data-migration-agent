"""Shared conflict detection for the two mapping analysers.

`analysis.build_analysis` (a single doctype) and
`customers_full.build_party_sheet_analysis` (flat party sheets) surface the same
classes of problem: source columns with no home, and Link values that do not
exist on the target site. The value-level checks and the conflict/suggestion
dict shapes live here so the two analysers cannot drift apart — and so the agent
sees one schema regardless of which flow produced the analysis.
"""
from __future__ import annotations

from typing import Optional

from .client import ERPNextClient
from .source import SourceTable

#: how many offending values we echo back into an analysis artifact
MAX_VALUES = 25


def distinct_values(source: SourceTable, header: str, cap: int = 50,
                    require_column: Optional[str] = None) -> list[str]:
    """Distinct non-empty values of `header`, in source order.

    `require_column` restricts the scan to rows that will actually import (e.g.
    rows carrying a party name). Without it, a junk value sitting in a row that
    is skipped anyway becomes a phantom conflict — and the agent would "fix" it
    by creating nonsense master data.
    """
    idx = source.column_index(header)
    if idx is None:
        return []
    req = source.column_index(require_column) if require_column else None
    seen: set[str] = set()
    out: list[str] = []
    for row in source.rows:
        if req is not None and (req >= len(row) or not str(row[req]).strip()):
            continue  # row has no identity -> skipped at import
        if idx < len(row):
            v = str(row[idx]).strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
                if len(out) >= cap:
                    break
    return out


def fieldtype_for(profile) -> str:
    """Best-guess ERPNext fieldtype for a source column profile."""
    if profile is None:
        return "Data"
    return {
        "int": "Int",
        "float": "Float",
        "date": "Date",
        "bool": "Check",
    }.get(profile.inferred_type, "Data")


class UnverifiableLink(Exception):
    """The linked doctype could not be queried, so values cannot be judged."""


def existing_values(client: ERPNextClient, doctype: str,
                    cache: Optional[dict] = None) -> set[str]:
    """Record names of `doctype`, memoised in `cache` (one query per doctype).

    Raises `UnverifiableLink` when the lookup itself fails. "I could not check"
    is NOT "nothing exists": degrading to an empty set makes *every* value look
    missing, producing a confident but unsatisfiable conflict — an agent then
    tries to create records in a doctype that does not exist.
    """
    if cache is not None and doctype in cache:
        cached = cache[doctype]
        if cached is None:
            raise UnverifiableLink(doctype)
        return cached
    try:
        names = {str(r.get("name"))
                 for r in client.list(doctype, fields=["name"], limit=0)}
    except Exception as e:
        if cache is not None:
            cache[doctype] = None      # remember the failure, don't re-query
        raise UnverifiableLink(f"{doctype}: {e}") from e
    if cache is not None:
        cache[doctype] = names
    return names


def missing_link_values(client: ERPNextClient, values: list[str],
                        linked_doctype: str,
                        cache: Optional[dict] = None) -> Optional[list[str]]:
    """Which of `values` are absent from `linked_doctype`.

    Returns `None` when the linked doctype could not be queried at all — callers
    must treat that as "unverifiable" and stay silent, never as "all missing".
    A genuinely bad link value still fails loudly at import time.
    """
    try:
        have = existing_values(client, linked_doctype, cache)
    except UnverifiableLink:
        return None
    return [v for v in values if v not in have]


def suggested_custom_field(source: str, doctype: str, fieldname: str,
                           fieldtype: str = "Data", reason: str = "") -> dict:
    """A ready-to-run `createfield` suggestion for an unmapped column."""
    return {
        "source": source,
        "doctype": doctype,
        "fieldname": fieldname,
        "label": source,
        "fieldtype": fieldtype,
        "reason": reason,
        "create_command": (
            f"python3 erpgen.py createfield {doctype} "
            f"--label '{source}' --fieldtype {fieldtype}"
        ),
    }


def unmapped_column_conflict(source: str, severity: str = "info", detail: str = "",
                             suggested_action: str = "", **extra) -> dict:
    """An `unmapped_column` conflict.

    `extra` carries flow-specific keys (e.g. the flat flow adds the matched
    `target`/`doctype`), so both analysers emit one shape with one implementation.
    """
    return {
        "kind": "unmapped_column",
        "severity": severity,
        "source": source,
        "detail": detail or ("Source column has no matching ERPNext field; "
                             "values are dropped."),
        "suggested_action": suggested_action or
        "create a custom field (see suggested_custom_fields) or ignore.",
        **extra,
    }


def link_value_conflict(source: str, targets, linked_doctype: str,
                        missing: list[str]) -> dict:
    """A `link_value_conflict` for one source column and its linked doctype.

    `targets` is every qualified field the column maps to (a column can mirror
    onto more than one doctype); `target` is kept as the first of them for
    consumers that expect a single value.
    """
    qualified = list(targets)
    return {
        "kind": "link_value_conflict",
        "severity": "error",
        "source": source,
        "target": qualified[0],
        "targets": qualified,
        "doctype": linked_doctype,
        "missing_values": missing[:MAX_VALUES],
        "detail": (
            f"{len(missing)} source value(s) for '{source}' do not exist "
            f"in {linked_doctype}."
        ),
        "suggested_action": (
            f"create the missing {linked_doctype} record(s) with create_record — "
            f"describe_doctype('{linked_doctype}') lists the required fields, "
            "including child-table ones — then re-run map."
        ),
    }
