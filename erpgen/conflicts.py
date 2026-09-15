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

from .cleaning_utils import (compare_key, levenshtein, token_signature,
                             within_distance)
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


#: Fields that reject a *Group* node and require a leaf. This is the only such
#: validation in ERPNext (`Customer.validate_customer_group`, which throws
#: "Cannot select a Group type Customer Group"): an Item and a Customer accept a
#: group node in `item_group` / `territory`, so treating every tree Link as
#: leaf-only would report conflicts ERPNext is perfectly happy with.
LEAF_ONLY_LINKS = {("Customer", "customer_group")}


def group_node_values(client: ERPNextClient, values: list[str],
                      linked_doctype: str,
                      cache: Optional[dict] = None) -> Optional[list[str]]:
    """Which of `values` are Group nodes (`is_group`) in `linked_doctype`.

    `None` when the doctype could not be queried — same unverifiable contract as
    `missing_link_values`, so an unreachable site stays silent rather than
    reporting every value. `cache` is shared with the value lookups, keyed by a
    prefixed string so the two never collide.
    """
    if not values:
        return []
    key = f"!!is_group::{linked_doctype}"
    if cache is not None and key in cache:
        nodes = cache[key]
        if nodes is None:
            return None
    else:
        try:
            rows = client.list(linked_doctype, fields=["name", "is_group"],
                               limit=0)
        except Exception:
            if cache is not None:
                cache[key] = None      # remember the failure, don't re-query
            return None
        nodes = {str(r.get("name")) for r in rows if r.get("is_group")}
        if cache is not None:
            cache[key] = nodes
    return [v for v in values if v in nodes]


def link_group_node(source: str, targets, linked_doctype: str,
                    nodes: list[str]) -> dict:
    """A Link value that exists, but is a Group node where a leaf is required.

    Distinct from `link_value_conflict`: the record is there, so "create the
    missing record" is the wrong fix and would loop the agent forever. The row
    has to point somewhere else.
    """
    qualified = list(targets)
    return {
        "kind": "link_group_node",
        "severity": "error",
        "source": source,
        "target": qualified[0],
        "targets": qualified,
        "doctype": linked_doctype,
        "group_values": sorted(nodes)[:MAX_VALUES],
        "detail": (
            f"{len(nodes)} source value(s) for '{source}' are Group nodes in "
            f"{linked_doctype}; ERPNext rejects a Group node here — this field "
            "needs a leaf."
        ),
        "suggested_action": (
            f"point the row at a leaf instead: pick an existing leaf under the "
            f"group, or create one (create_record '{linked_doctype}' with its "
            f"parent set and is_group 0). The value lives in the source, so record "
            f"a set_value correction on '{source}' that writes the leaf's name."
        ),
    }


# ------------------------------------------------------- data quality checks
def _text(row: list, idx: int) -> str:
    """Cell value as text; missing cells and None read as empty."""
    if idx >= len(row):
        return ""
    cell = row[idx]
    return "" if cell is None else str(cell)


def duplicate_key_groups(
    source: SourceTable,
    key_column: str,
    compare_columns: Optional[list[str]] = None,
    cap: int = MAX_VALUES,
) -> tuple[list[dict], int]:
    """Rows that share a key value, grouped by the casefolded, stripped key.

    Returns (groups, group_count). `groups` is capped; `group_count` is the true
    number of duplicated keys, so a capped report still states the real total.
    Rows with an empty key are not groups — `data_quality_conflicts` reports those
    as `missing_value` instead.

    `compare_columns` decides what "identical" means: when omitted (a sheet-only
    run with no mapping), every other column is compared, which can only ever
    report *more* differences, never fewer.
    """
    idx = source.column_index(key_column)
    if idx is None:
        return [], 0

    names = list(compare_columns) if compare_columns is not None else list(source.headers)
    compare: list[tuple[str, int]] = []
    for name in names:
        if name == key_column:
            continue
        j = source.column_index(name)
        if j is not None:
            compare.append((name, j))

    order: list[str] = []
    groups: dict[str, dict] = {}

    for i, row in enumerate(source.rows):
        cell = _text(row, idx)
        raw = cell.strip()
        if not raw:
            continue
        gkey = raw.casefold()
        g = groups.get(gkey)
        if g is None:
            g = {
                "key_value": gkey,
                "values": [],
                "rows": [],
                "count": 0,
                "differing": [],
                "first": tuple(_text(row, j).strip() for _, j in compare),
            }
            groups[gkey] = g
            order.append(gkey)
        g["count"] += 1
        g["rows"].append(i + 2)                 # spreadsheet row: header is row 1
        if cell not in g["values"]:
            g["values"].append(cell)          # as read: case/space variants preserved
        for (name, j), prev in zip(compare, g["first"]):
            if name not in g["differing"] and _text(row, j).strip() != prev:
                g["differing"].append(name)

    out: list[dict] = []
    total = 0
    for gkey in order:
        g = groups[gkey]
        if g["count"] < 2:
            continue
        total += 1
        if len(out) >= cap:
            continue
        raw_values = g["values"]
        out.append({
            "key_value": gkey,
            "values": raw_values[:cap],
            "rows": g["rows"][:cap],
            "count": g["count"],
            "identical": not g["differing"],
            "case_variant": len({v.strip() for v in raw_values}) > 1,
            "whitespace_variant": any(v != v.strip() for v in raw_values),
            "differing_fields": g["differing"],
        })
    return out, total


def duplicate_row_conflict(
    key_column: str,
    groups: list[dict],
    group_count: int,
    key_field: str = "",
) -> dict:
    """One `duplicate_row` conflict for a key column.

    Aggregated per column rather than per duplicated value: requirement identity
    is `(kind, source, target, doctype, field)`, so per-value conflicts would be
    deduped away as "already recorded" (see `context._identity_of`).
    """
    differing = [g for g in groups if not g["identical"]]
    affected = sum(g["count"] for g in groups)
    if differing:
        detail = (f"{group_count} key value(s) appear on more than one row "
                  f"({affected} row(s)); {len(differing)} group(s) have differing "
                  f"values, which ERPNext would import as a second record named "
                  f"\"<key> - 1\".")
        action = ("merge the rows (merge_rows, choosing the cells that survive), "
                  "drop the extra row (skip_row), or retarget the review key with "
                  "change_key when the wrong column was guessed.")
    else:
        detail = (f"{group_count} key value(s) appear on more than one row "
                  f"({affected} row(s)); the rows are identical, so the extra "
                  f"ones are skipped at import.")
        action = "no action needed unless these are genuinely separate entities."
    return {
        "kind": "duplicate_row",
        "severity": "error" if differing else "warning",
        "source": key_column,
        "target": key_field or "",
        "groups": groups,
        "group_count": group_count,
        "detail": detail,
        "suggested_action": action,
    }


def missing_value_conflict(
    column: str,
    target: str,
    roles: list[str],
    rows: list[int],
    count: int,
    cap: int = MAX_VALUES,
) -> dict:
    """One `missing_value` conflict for a column.

    `roles` carries every reason the column matters: `key` (the row has no
    identity) and/or `required` (ERPNext rejects the row). One conflict per
    column, never two, because both roles share an identity tuple.
    """
    what = "the key column" if "key" in roles else f"required field '{target or column}'"
    return {
        "kind": "missing_value",
        "severity": "error",
        "source": column,
        "target": target or "",
        "roles": list(roles),
        "rows": rows[:cap],
        "count": count,
        "detail": f"{count} row(s) have no value in {what}.",
        "suggested_action": (
            "fill the value in the source sheet or record it as a worksheet "
            "correction; for a required field a constant can also come from "
            "--defaults, and a blank key row is dropped at import."
        ),
    }


def required_mapped_columns(parent, plan) -> list[tuple[str, str]]:
    """(source column, target field) for required fields a column feeds.

    Required fields with no column at all are a different conflict —
    `required_missing`, raised by `analysis.build_analysis`. A field covered by
    `plan.defaults` is not flagged: the default fills it.
    """
    out: list[tuple[str, str]] = []
    for f in parent.mandatory_fields():
        if f.fieldname in plan.defaults or f.is_fetch_field:
            continue
        column = next((m.source for m in plan.mappings if m.target == f.fieldname), None)
        if column:
            out.append((column, f.fieldname))
    return out


def data_quality_conflicts(
    source: SourceTable,
    *,
    key_column: Optional[str] = None,
    key_field: str = "",
    required: Optional[list[tuple[str, str]]] = None,
    compare_columns: Optional[list[str]] = None,
    cap: int = MAX_VALUES,
) -> list[dict]:
    """The cleaning-stage conflicts for one sheet: one entry per key column and
    per tracked required column, in a deterministic order.

    Pure: no client, no network, no clock.
    """
    conflicts: list[dict] = []

    tracked: dict[str, dict] = {}
    if key_column:
        tracked[key_column] = {"target": key_field or "", "roles": ["key"]}
    for column, target in (required or []):
        entry = tracked.setdefault(column, {"target": target or "", "roles": []})
        if "required" not in entry["roles"]:
            entry["roles"].append("required")
        if not entry["target"]:
            entry["target"] = target or ""

    if key_column:
        groups, total = duplicate_key_groups(source, key_column, compare_columns, cap)
        if total:
            conflicts.append(duplicate_row_conflict(key_column, groups, total, key_field))

    for column, entry in tracked.items():
        idx = source.column_index(column)
        if idx is None:
            continue
        rows = [i + 2 for i, row in enumerate(source.rows) if not _text(row, idx).strip()]
        if rows:
            conflicts.append(missing_value_conflict(
                column, entry["target"], entry["roles"], rows, len(rows), cap))

    return conflicts


# ------------------------------------------------------ possible duplicates
#: Bounded edit distance for signal A: catches a one- or two-character typo while
#: still rejecting a genuinely different name.
NEAR_DUP_K = 2

#: Signal A is all-pairs (`O(rows²)`), measured at ~14 s for 2,000 rows. Above this
#: it is skipped and signal B — which is `O(rows)` — still runs.
MAX_PAIRS_ROWS = 2000

#: A shared value is evidence of a duplicate row only when the column is
#: near-unique in this sheet (`customer_group` is shared legitimately), well
#: populated (a sparse free-text column like `Notes` is near-unique by nature) and
#: textual (a credit limit is near-unique and populated, but sharing one means
#: nothing).
IDENTIFIER_UNIQUE = 0.9
IDENTIFIER_POPULATED = 0.5
MAX_IDENTIFIER_COLUMNS = 5

#: fixed order, so `signals` never depends on discovery order
SIGNAL_ORDER = ("key_fuzzy", "token_reorder", "shared_identifier")


def identifier_columns(source: SourceTable, key_column: Optional[str],
                       cap: int = MAX_IDENTIFIER_COLUMNS) -> list[str]:
    """Columns whose shared value suggests two rows are the same entity."""
    out: list[str] = []
    for profile in source.profiles:
        header = profile.header
        if header == key_column or header in out:
            continue
        if profile.inferred_type != "text":
            continue
        if profile.unique < IDENTIFIER_UNIQUE or profile.non_empty < IDENTIFIER_POPULATED:
            continue
        out.append(header)
        if len(out) >= cap:
            break
    return out


def possible_duplicate_pairs(
    source: SourceTable,
    key_column: str,
    *,
    compare_columns: Optional[list[str]] = None,
    identifiers: Optional[list[str]] = None,
    k: int = NEAR_DUP_K,
    max_pairs_rows: int = MAX_PAIRS_ROWS,
    cap: int = MAX_VALUES,
) -> tuple[list[dict], int, bool]:
    """Row pairs whose keys look like one entity spelled two ways.

    Returns `(pairs, pair_count, signal_a_skipped)`. `pair_count` is the true total
    while `pairs` is capped, and pairs are ordered by spreadsheet row so the report
    is stable across runs.

    A pair is reported when **either** route holds:

    * **signal A** — `within_distance(compare_key(a), compare_key(b), k)`, or the
      two share a token signature (reordered words)
    * **signal B** — they share an exact value in an `identifier_columns` column

    Signal B is exempt from the fuzzy confirmation on purpose: a shared email under
    differently spelled names is exactly what signal A cannot see, and it is
    stronger evidence than a one-character difference.

    Pairs with *equal* keys are excluded — those belong to `duplicate_row`, and
    reporting them twice would double-count one defect.
    """
    if not key_column:
        return [], 0, False
    key_idx = source.column_index(key_column)
    if key_idx is None:
        return [], 0, False

    # (orig_index, row_no, raw, compare_key, token_signature, dup_key) — the
    # original index is kept because blank-key rows are filtered out and positions
    # shift. `dup_key` is `duplicate_row`'s grouping key, NOT the comparison key:
    # `"Acme Steel"` and `"Acme Steel Pte Ltd"` share a comparison key but are
    # different groups for `duplicate_row`, so they are a near-duplicate here.
    rows: list[tuple[int, int, str, str, str, str]] = []
    for i, row in enumerate(source.rows):
        raw = _text(row, key_idx).strip()
        if not raw:
            continue                       # a blank key is `missing_value`'s job
        rows.append((i, i + 2, raw, compare_key(raw), token_signature(raw),
                     raw.casefold()))

    candidates: dict[tuple[int, int], dict] = {}     # insertion-ordered

    def pair(i: int, j: int) -> dict:
        entry = candidates.get((i, j))
        if entry is None:
            entry = {"signals": []}
            candidates[(i, j)] = entry
        return entry

    skipped = len(rows) > max_pairs_rows
    if not skipped:
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                if rows[i][5] == rows[j][5]:
                    continue                 # same group: `duplicate_row` has it
                if within_distance(rows[i][3], rows[j][3], k):
                    pair(i, j)["signals"].append("key_fuzzy")
                if rows[i][4] == rows[j][4]:
                    entry = pair(i, j)
                    if "token_reorder" not in entry["signals"]:
                        entry["signals"].append("token_reorder")

    columns = identifier_columns(source, key_column) if identifiers is None else identifiers
    for column in columns:
        idx = source.column_index(column)
        if idx is None:
            continue
        first_seen: dict[str, int] = {}
        for pos, (orig, _no, _raw, _key, _sig, dup_key) in enumerate(rows):
            value = _text(source.rows[orig], idx).strip()
            if not value:
                continue                     # a blank is not a shared identifier
            first = first_seen.get(value)
            if first is None:
                first_seen[value] = pos      # pairs are formed against the first
                continue                     # occurrence, so 3 rows give 2 pairs
            if rows[first][5] == dup_key:
                continue                     # same group: `duplicate_row` has it
            entry = pair(first, pos)
            if "shared_identifier" not in entry["signals"]:
                entry["signals"].append("shared_identifier")
            entry.setdefault("shared_fields", []).append(column)

    compared = [(name, source.column_index(name))
                for name in (compare_columns if compare_columns is not None
                             else source.headers)
                if name != key_column]
    compared = [(name, idx) for name, idx in compared if idx is not None]

    out: list[dict] = []
    for (i, j) in sorted(candidates):
        entry = candidates[(i, j)]
        out.append({
            "a": {"row": rows[i][1], "value": rows[i][2]},
            "b": {"row": rows[j][1], "value": rows[j][2]},
            "edit_distance": levenshtein(rows[i][3], rows[j][3]),
            "signals": sorted(set(entry["signals"]), key=SIGNAL_ORDER.index),
            "shared_fields": sorted(set(entry.get("shared_fields", []))),
            "differing_fields": [
                name for name, idx in compared
                if _text(source.rows[rows[i][0]], idx).strip()
                != _text(source.rows[rows[j][0]], idx).strip()],
        })

    return out[:cap], len(candidates), skipped


def possible_duplicate_row_conflict(
    key_column: str,
    pairs: list[dict],
    pair_count: int,
    key_field: str = "",
    skipped_signal_a: bool = False,
) -> dict:
    """One `possible_duplicate_row` conflict for a key column.

    Always `warning`: `"Acme Steel"` in two cities may be two genuine entities, so
    this is a review list, never a gate. Aggregated per key column like
    `duplicate_row`, for the same requirement-identity reason.
    """
    detail = (f"{pair_count} pair(s) of rows may be one entity spelled two ways.")
    if skipped_signal_a:
        detail += (f" Name similarity was skipped: more than {MAX_PAIRS_ROWS} rows "
                   f"and that comparison is quadratic.")
    return {
        "kind": "possible_duplicate_row",
        "severity": "warning",
        "source": key_column,
        "target": key_field or "",
        "pairs": pairs,
        "pair_count": pair_count,
        "signal_a_skipped": bool(skipped_signal_a),
        "detail": detail,
        "suggested_action": (
            "review each pair: merge them (merge_rows), unify the spelling "
            "(set_value), or dismiss the conflict if they are genuinely separate "
            "entities."
        ),
    }
