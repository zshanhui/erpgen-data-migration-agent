"""Employee-specific mapping: the source's employee number.

ERPNext names an Employee from `naming_series` (`HR-EMP-#####`) and keeps the
legacy key in `employee_number`, so the ID column never maps on its own:

* `Emp ID` / `Emp No` / `Staff ID` share no token with `employee_number`;
* `Employee ID` scores 0.85 against `employee` — which `validate()` overwrites
  with `self.name` — and 0.85 beats a 0.78 synonym entry.

So the decision is forced here, after scoring and after `mapping-overrides.json`
(which still wins). The same field is the dedup key, because `employee_name`
cannot be one: ERPNext recomputes it from first/middle/last, so a re-run would
not recognise its own records.
"""
from __future__ import annotations

import re
from typing import Optional

from .mapper import ColumnMapping, MappingEngine, MappingPlan, norm
from .source import SourceTable

ID_FIELD = "employee_number"

#: normalised header -> it means the employee's own number. The spelled-out
#: `Employee Number` is listed too: the scorer matches it, but this table is
#: what decides that the column is the ID at all.
ALIASES = frozenset({
    "empid", "employeeid", "empno", "employeeno", "employeenumber",
    "staffid", "staffno", "staffnumber", "empcode", "employeecode",
    "personnelnumber", "badgeid", "badgeno", "badgenumber",
})

#: `EMP-001` -> ("EMP-", "001"), used to continue a sheet's own numbering
_NUMBERED = re.compile(r"^(.*?)(\d+)$")

#: no sheet numbering to continue from
_DEFAULT_PREFIX, _DEFAULT_WIDTH = "EMP-", 3


def id_column(source: SourceTable) -> Optional[str]:
    """The source column carrying the employee ID, matched by header alias."""
    for header in source.headers:
        if norm(header) in ALIASES:
            return header
    return None


def apply(engine: MappingEngine, plan: MappingPlan,
          source: SourceTable) -> list[str]:
    """Force the ID column onto `employee_number`; returns log lines.

    A no-op for any other doctype, and when the target field does not exist.
    """
    if plan.doctype != "Employee" or not engine.parent.get(ID_FIELD):
        return []

    column = id_column(source)
    if column is None:
        return [f"no employee ID column found in the headers; every row will get "
                f"a generated {ID_FIELD}"]

    mapping = next((m for m in plan.mappings if m.source == column), None)
    if mapping is None:
        mapping = ColumnMapping(column, None, 0.0, "none")
        plan.mappings.append(mapping)
    previous = mapping.target
    mapping.target = ID_FIELD
    mapping.confidence = 1.0
    mapping.method = "employee_id"
    mapping.alternatives = []
    mapping.notes = [n for n in mapping.notes if not n.startswith("ambiguous with")]
    engine.check_coverage(plan)

    line = f"'{column}' -> {ID_FIELD} (employee ID column, and the dedup key)"
    if previous and previous != ID_FIELD:
        line += f" [was {previous!r}]"
    return [line]


def ensure_ids(payloads: list[dict]) -> list[str]:
    """Give every payload an `employee_number`, generating the missing ones.

    Both "not present" cases land here: a sheet with no ID column at all, and a
    column with blank cells. Generated values continue the sheet's own numbering
    and skip anything already used (`EMP-026`, `EMP-027`, …), so re-running the
    same file yields the same values and dedup recognises them. The source sheet
    is never modified. Caveat: generated values are not checked against what the
    site already holds, only against this sheet.
    """
    present = [str(p.get(ID_FIELD) or "").strip() for p in payloads]
    missing = [p for p, value in zip(payloads, present) if not value]
    if not missing:
        return []

    used = {value for value in present if value}
    prefix, width, number = _numbering([v for v in present if v])
    first = ""
    for p in missing:
        while True:
            number += 1
            value = f"{prefix}{number:0{width}d}"
            if value not in used:
                break
        p[ID_FIELD] = value
        used.add(value)
        first = first or value

    rows = ", ".join(str(p.get("__row")) for p in missing[:5])
    if len(missing) > 5:
        rows += ", …"
    return [f"generated {len(missing)} {ID_FIELD} value(s) from {first} "
            f"(rows {rows})"]


def _numbering(values: list[str]) -> tuple[str, int, int]:
    """`(prefix, width, highest)` continued from the sheet's own values.

    The first numbered value sets the shape, so a sheet numbering `STAFF-0007`
    yields `STAFF-0008` rather than `EMP-001`. Falls back to `EMP-`/3.
    """
    for value in values:
        m = _NUMBERED.match(value)
        if not m:
            continue
        prefix = m.group(1)
        same = [g for g in (_NUMBERED.match(v) for v in values)
                if g and g.group(1) == prefix]
        return prefix, max(len(g.group(2)) for g in same), max(int(g.group(2)) for g in same)
    return _DEFAULT_PREFIX, _DEFAULT_WIDTH, 0
