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

from .conflicts import distinct_values
from .mapper import ColumnMapping, MappingEngine, MappingPlan, norm
from .metadata import DoctypeMeta
from .source import SourceTable
from .tools import create_field, create_record

ID_FIELD = "employee_number"

#: normalised header -> it means the employee's own number. The spelled-out
#: `Employee Number` is listed too: the scorer matches it, but this table is
#: what decides that the column is the ID at all.
ALIASES = frozenset({
    "empid", "employeeid", "empno", "employeeno", "employeenumber",
    "staffid", "staffno", "staffnumber", "empcode", "employeecode",
    "personnelnumber", "badgeid", "badgeno", "badgenumber",
})

#: normalised header -> the person's full name, kept whole. ERPNext recomputes
#: `employee_name` from first/middle/last (`set_employee_name`), so a single
#: full-name column has to land in `first_name`; writing `employee_name` is a
#: no-op that also leaves `first_name` empty — the required field.
NAME_ALIASES = frozenset({"fullname", "employeename"})

#: normalised header -> its own Employee field, for columns with no built-in
#: home. Left to the scorer, `Bank Branch` lands on `branch` — the office-branch
#: Link — and creates bogus Branch records.
OWN_FIELDS = {
    "bankbranch": ("bank_branch", "Bank Branch", "Data"),
}

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


def own_field(header: str) -> Optional[tuple[str, str, str]]:
    """`(fieldname, label, fieldtype)` for a column that needs its own field."""
    return OWN_FIELDS.get(norm(header))


def _force(engine: MappingEngine, plan: MappingPlan, source: SourceTable,
           column: str, target: str, method: str) -> None:
    """Point `column` at `target` after scoring, clearing the ambiguity."""
    mapping = next((m for m in plan.mappings if m.source == column), None)
    if mapping is None:
        mapping = ColumnMapping(column, None, 0.0, "none")
        plan.mappings.append(mapping)
    mapping.target = target
    mapping.confidence = 1.0
    mapping.method = method
    mapping.alternatives = []
    mapping.notes = [n for n in mapping.notes if not n.startswith("ambiguous with")]


def apply(engine: MappingEngine, plan: MappingPlan, source: SourceTable,
          client=None) -> list[str]:
    """Force the employee-specific mappings; returns log lines.

    A no-op for any other doctype. Ordering and duplicate handling are left to
    `_force`, which mirrors what `mapping-overrides.json` does — except these are
    employee facts, so they live in code.
    """
    if plan.doctype != "Employee":
        return []
    lines: list[str] = []
    forced = False

    if engine.parent.get(ID_FIELD):
        column = id_column(source)
        if column is None:
            lines.append(f"no employee ID column found in the headers; every row "
                         f"will get a generated {ID_FIELD}")
        else:
            previous = _target_of(plan, column)
            _force(engine, plan, source, column, ID_FIELD, "employee_id")
            line = f"'{column}' -> {ID_FIELD} (employee ID column, and the dedup key)"
            if previous and previous != ID_FIELD:
                line += f" [was {previous!r}]"
            lines.append(line)
            forced = True

    for header in source.headers:
        if norm(header) not in NAME_ALIASES or not engine.parent.get("first_name"):
            continue
        previous = _target_of(plan, header)
        _force(engine, plan, source, header, "first_name", "employee_name")
        line = (f"'{header}' -> first_name (kept whole; ERPNext recomputes "
                f"employee_name from it)")
        if previous and previous != "first_name":
            line += f" [was {previous!r}]"
        lines.append(line)
        forced = True

    for header in source.headers:
        spec = own_field(header)
        if not spec:
            continue
        fieldname = spec[0]
        if not engine.parent.get(fieldname):
            lines.append(f"'{header}' has no Employee field yet; it gets "
                         f"'{fieldname}' on import --apply, and is dropped until "
                         f"then")
        # once the field exists the scorer matches it by label, so nothing to
        # force here

    if forced:
        engine.check_coverage(plan)
    lines.extend(_department_lines(plan, source, engine, client))
    return lines


def _target_of(plan: MappingPlan, column: str) -> Optional[str]:
    mapping = next((m for m in plan.mappings if m.source == column), None)
    return mapping.target if mapping else None


def _mapped_source(plan: MappingPlan, target: str) -> Optional[str]:
    for m in plan.mappings:
        if m.target == target:
            return m.source
    return None


# --------------------------------------------------------------- department
#: normalised headers for the columns the Department mapping needs
DEPARTMENT_ALIASES = frozenset({"department", "dept"})
COMPANY_ALIASES = frozenset({"company"})


def _alias_column(source: SourceTable, aliases: frozenset) -> Optional[str]:
    for header in source.headers:
        if norm(header) in aliases:
            return header
    return None


def department_plan(source: SourceTable, client) -> tuple[dict[str, str], list[dict]]:
    """`({sheet value: Department docname}, [records to create])`.

    A Department docname is `get_abbreviated_name`: `<department_name> - <company
    abbr>`, and `company` is required, so the sheet's bare `Management` never
    matches the `Management - DM` the Link needs. A Department already carrying
    the name wins, so one created outside this tool is reused, not duplicated.
    Remapping happens through a value_map — the source is never edited.
    """
    column = _alias_column(source, DEPARTMENT_ALIASES)
    if not column or client is None:
        return {}, []
    values = distinct_values(source, column)
    if not values:
        return {}, []
    company = _single_company(source)
    abbr = _company_abbr(client, company)
    if not abbr:
        return {}, []
    existing = _department_docnames(client)

    mapping: dict[str, str] = {}
    to_create: list[dict] = []
    for value in values:
        docname = existing.get(value)
        if docname:
            mapping[value] = docname
        else:
            mapping[value] = f"{value} - {abbr}"
            to_create.append({"department_name": value, "company": company})
    return mapping, to_create


def _single_company(source: SourceTable) -> str:
    """The sheet's company, only when there is exactly one: a value_map is keyed
    by value, so it cannot carry a different company per row."""
    column = _alias_column(source, COMPANY_ALIASES)
    values = distinct_values(source, column) if column else []
    return values[0] if len(values) == 1 else ""


def _company_abbr(client, company: str) -> str:
    if not company:
        return ""
    try:
        rows = client.list("Company", filters=[["name", "=", company]],
                           fields=["abbr"], limit=1)
    except Exception:  # noqa: BLE001 — stay silent, the Link conflict still shows
        return ""
    return str(rows[0].get("abbr") or "") if rows else ""


def _department_docnames(client) -> dict[str, str]:
    """`{department_name: docname}` for every Department on the site."""
    try:
        rows = client.list("Department", fields=["name", "department_name"],
                           limit=0)
    except Exception:  # noqa: BLE001
        return {}
    return {str(r.get("department_name")): str(r.get("name"))
            for r in rows if r.get("department_name") and r.get("name")}


def _department_lines(plan: MappingPlan, source: SourceTable,
                      engine: MappingEngine, client) -> list[str]:
    """Install the Department value_map into the plan (in memory, no writes)."""
    if not engine.parent.get("department"):
        return []
    mapping, to_create = department_plan(source, client)
    if not mapping:
        return []
    installed = plan.value_maps.setdefault("department", {})
    added = {value: target for value, target in mapping.items()
             if value not in installed}   # a recorded override wins
    if not added:
        return []
    installed.update(added)
    shown = ", ".join(f"{v} -> {t}" for v, t in list(added.items())[:3])
    if len(added) > 3:
        shown += ", …"
    line = (f"Department: remapped {len(added)} value(s) to Department docnames "
            f"({shown})")
    if to_create:
        names = ", ".join(c["department_name"] for c in to_create[:3])
        line += (f"; {len(to_create)} missing Department(s) ({names}) are created "
                 f"on import --apply")
    return [line]


# ------------------------------------------------------------- prerequisites
def prerequisites(source: SourceTable, client, journal=None) -> list[str]:
    """Create what the mapping needs *before* the analysis is built.

    `import --apply` only. Both parts have to land this early: a custom field
    must exist before `MappingEngine` fetches the doctype (targets are matched by
    fieldname, so a column pointing at a field that is not there is dropped
    without a conflict), and a Department the value_map targets must exist before
    the link check runs, or it reports the mapped value as missing and the gate
    refuses the very run that would create it.
    """
    lines: list[str] = []
    for header in source.headers:
        spec = own_field(header)
        if not spec:
            continue
        fieldname, label, fieldtype = spec
        result = create_field(client, "Employee", label=label,
                              fieldtype=fieldtype, fieldname=fieldname)
        if result.get("created"):
            if journal is not None:
                journal.custom_field_created("Employee", fieldname,
                                             str(result.get("name") or ""),
                                             label=label)
            lines.append(f"created field '{fieldname}' ({label}) on Employee")
        else:
            lines.append(f"field '{fieldname}' already exists on Employee")
        if not DoctypeMeta.fetch(client, "Employee").get(fieldname):
            lines.append(f"WARNING: '{fieldname}' is not visible in the Employee "
                         f"metadata yet, so '{header}' cannot be mapped this run")
    lines.extend(create_missing_departments(source, client, journal))
    return lines


def create_missing_departments(source: SourceTable, client,
                               journal=None) -> list[str]:
    """Create the Departments the value_map points at."""
    _mapping, to_create = department_plan(source, client)
    lines: list[str] = []
    for fields in to_create:
        name = fields["department_name"]
        try:
            result = create_record(client, "Department", fields)
        except Exception as e:  # noqa: BLE001 — report, never abort the run
            lines.append(f"WARNING: Department '{name}' could not be created: {e}")
            continue
        docname = str(result.get("name") or "")
        if result.get("created"):
            if journal is not None:
                journal.record_created("Department", docname)
            lines.append(f"created Department '{docname}' for '{name}'")
    return lines


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
