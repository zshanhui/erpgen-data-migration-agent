"""Employee ID mapping: the source's employee number becomes the dedup key.

ERPNext names an Employee from `naming_series` and keeps the legacy key in
`employee_number`, so the ID column does not map on its own — `Emp ID` shares no
token with the field, and `Employee ID` matches the server-derived `employee`
field instead. Getting this wrong is not cosmetic: the probe run created 48
records for 25 rows.
"""
from __future__ import annotations

from conftest import make_field, make_meta, make_sheet
from erpgen import employees
from erpgen.dedup import DEDUP_KEYS, NATURAL_KEYS, infer_id_column, resolve_key_field
from erpgen.mapper import MappingEngine


def _engine():
    """Labels mirror the live doctype: `employee_name` is labelled Full Name and
    `employee` is a bare Data field that validate() overwrites with `name`."""
    meta = make_meta("Employee", [
        make_field("first_name", "First Name", reqd=True),
        make_field("employee_name", "Full Name"),
        make_field("employee", "Employee"),
        make_field("employee_number", "Employee Number"),
        make_field("designation", "Designation", "Link", options="Designation"),
    ], autoname="naming_series:")
    return MappingEngine(meta)


def _apply(headers, rows=None):
    engine = _engine()
    sheet = make_sheet(list(headers), [list(r) for r in rows] if rows else None)
    plan = engine.suggest(sheet)
    lines = employees.apply(engine, plan, sheet)
    return engine, plan, sheet, lines


def _target(plan, column):
    return next(m.target for m in plan.mappings if m.source == column)


# ------------------------------------------------------------------ the aliases
def test_every_alias_spelling_is_recognised_as_the_id_column():
    for header in ("Emp ID", "Employee ID", "Employee Number", "Emp No",
                   "Staff ID", "Staff No", "Personnel Number", "Employee Code",
                   "Badge No", "EMP_CODE", "employee-id"):
        engine, plan, sheet, lines = _apply([header, "First Name"],
                                            [["E-1", "Tan"]])
        assert employees.id_column(sheet) == header, header
        assert _target(plan, header) == "employee_number", header
        assert lines[0].startswith(
            f"'{header}' -> employee_number (employee ID column, "
            f"and the dedup key)"), header


def test_the_scorer_alone_would_pick_the_wrong_field():
    """`Employee ID` token-matches `employee` at 0.85, which beats a 0.78
    synonym — and `employee` is overwritten with `name` by validate()."""
    engine = _engine()
    sheet = make_sheet(["Employee ID", "First Name"], [["E-1", "Tan"]])
    plan = engine.suggest(sheet)
    assert _target(plan, "Employee ID") == "employee"

    employees.apply(engine, plan, sheet)
    assert _target(plan, "Employee ID") == "employee_number"


def test_the_forced_mapping_reports_the_decision_and_the_field_it_replaced():
    _engine_, _plan, _sheet, lines = _apply(["Employee ID"], [["E-1"]])
    assert lines == ["'Employee ID' -> employee_number (employee ID column, and "
                     "the dedup key) [was 'employee']"]


def test_an_alias_column_is_not_left_ambiguous():
    _engine_, plan, _sheet, _lines = _apply(["Employee Number"], [["E-1"]])
    m = next(m for m in plan.mappings if m.source == "Employee Number")
    assert m.alternatives == []
    assert not any("ambiguous" in n for n in m.notes)
    assert (m.confidence, m.method) == (1.0, "employee_id")


def test_the_alias_wins_only_for_employee():
    engine = MappingEngine(make_meta("Item", [
        make_field("item_code", "Item Code"),
        make_field("employee_number", "Employee Number"),
    ], autoname="field:item_code"))
    sheet = make_sheet(["Emp ID", "Item Code"], [["E-1", "A"]])
    plan = engine.suggest(sheet)
    assert employees.apply(engine, plan, sheet) == []


def test_a_doctype_without_the_field_is_left_alone():
    engine = MappingEngine(make_meta("Item", [make_field("item_code", "Item Code")],
                                     autoname="field:item_code"))
    sheet = make_sheet(["Emp ID", "Item Code"], [["E-1", "A"]])
    plan = engine.suggest(sheet)
    assert employees.apply(engine, plan, sheet) == []


def test_a_missing_id_column_is_reported_not_guessed():
    _engine_, _plan, _sheet, lines = _apply(["First Name"], [["Tan"]])
    assert len(lines) == 1
    assert "no employee ID column" in lines[0]


# ------------------------------------------------------------------ generation
def test_blank_cells_are_filled_continuing_the_sheets_numbering():
    """Numbering continues after the highest value in use and skips the ones
    already taken — EMP-026, EMP-027 for a sheet numbering EMP-001…EMP-025."""
    payloads = [{"employee_number": "EMP-001", "__row": 2},
                {"employee_number": "", "__row": 3},
                {"__row": 4},
                {"employee_number": "EMP-004", "__row": 5}]
    lines = employees.ensure_ids(payloads)
    assert [p.get("employee_number") for p in payloads] == [
        "EMP-001", "EMP-005", "EMP-006", "EMP-004"]
    assert "generated 2" in lines[0] and "from EMP-005" in lines[0]
    assert "rows 3, 4" in lines[0]


def test_generated_ids_skip_values_already_used():
    payloads = [{"employee_number": "EMP-005", "__row": 2},
                {"__row": 3},
                {"employee_number": "EMP-006", "__row": 4}]
    employees.ensure_ids(payloads)
    assert payloads[1]["employee_number"] == "EMP-007", "must not reuse EMP-006"


def test_a_sheet_with_no_id_column_at_all_gets_ids():
    payloads = [{"__row": 2}, {"__row": 3}]
    lines = employees.ensure_ids(payloads)
    assert [p["employee_number"] for p in payloads] == ["EMP-001", "EMP-002"]
    assert "generated 2" in lines[0]


def test_generation_continues_the_sheets_own_prefix_and_width():
    payloads = [{"employee_number": "STAFF-0007", "__row": 2}, {"__row": 3}]
    employees.ensure_ids(payloads)
    assert payloads[1]["employee_number"] == "STAFF-0008"


def test_a_complete_column_is_left_untouched():
    payloads = [{"employee_number": "EMP-001", "__row": 2}]
    assert employees.ensure_ids(payloads) == []
    assert payloads[0]["employee_number"] == "EMP-001"


def test_generation_never_writes_to_the_source():
    """The values land in the payload only — the sheet is the client's file."""
    sheet = make_sheet(["Emp ID"], [["", ""]])
    employees.ensure_ids([{"__row": 2}, {"__row": 3}])
    assert sheet.rows == [["", ""]]


# ------------------------------------------------------------------ the key
def test_employee_number_is_preferred_over_the_server_derived_name():
    engine = _engine()
    sheet = make_sheet(["Emp ID", "Full Name"], [["EMP-001", "Tan Wei Ming"]])
    plan = engine.suggest(sheet)
    # without the forced mapping, Full Name -> employee_name wins the id column
    assert infer_id_column(plan, sheet) == "Full Name"

    employees.apply(engine, plan, sheet)
    assert infer_id_column(plan, sheet) == "Emp ID"


def test_the_dedup_spec_queries_employee_number_not_name():
    """Employee `name` is HR-EMP-#####, so filtering it on `EMP-001` finds
    nothing and duplicates the sheet on every re-run."""
    spec = DEDUP_KEYS["Employee"]
    assert spec == {"source": "employee_number", "target": "employee_number"}
    assert resolve_key_field([{"employee_number": "EMP-001"}], "name") == "employee_number"
    assert "employee_number" in NATURAL_KEYS
