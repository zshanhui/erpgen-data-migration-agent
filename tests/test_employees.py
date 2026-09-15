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
        make_field("company", "Company", "Link", options="Company"),
        make_field("department", "Department", "Link", options="Department"),
        make_field("designation", "Designation", "Link", options="Designation"),
    ], autoname="naming_series:")
    return MappingEngine(meta)


class _Site:
    """Only what the employee helpers touch: `doctype_meta`, `list`, `insert`."""

    def __init__(self, companies=None, departments=(), custom_fields=(),
                 employee_fields=None):
        self.companies = companies or {}
        self.departments = {d["name"]: d for d in departments}
        self.custom_fields = list(custom_fields)
        self.employee_fields = employee_fields or [
            make_field("first_name", "First Name", reqd=True).as_dict(),
            make_field("employee_number", "Employee Number").as_dict(),
        ]
        self.inserted: list[tuple[str, dict]] = []

    def doctype_meta(self, doctype):
        fields = (self.employee_fields if doctype == "Employee"
                  else [make_field("department_name", "Department Name", reqd=True).as_dict(),
                        make_field("company", "Company", "Link", options="Company").as_dict()])
        return {"name": doctype, "autoname": None, "istable": 0,
                "is_submittable": 0, "fields": fields}

    def list(self, doctype, filters=None, fields=None, limit=0, order_by=None):
        if doctype == "Company":
            return [{"name": n, "abbr": a} for n, a in self.companies.items()]
        if doctype == "Department":
            return [{"name": d["name"], "department_name": d["department_name"]}
                    for d in self.departments.values()]
        if doctype == "Custom Field":
            return [{"name": f"Employee-{c['fieldname']}", **c}
                    for c in self.custom_fields]
        return []

    def insert(self, doctype, doc):
        self.inserted.append((doctype, doc))
        if doctype == "Department":
            name = f"{doc['department_name']} - DM"      # ERPNext autoname
            self.departments[name] = {"name": name, **doc}
        elif doctype == "Custom Field":
            self.custom_fields.append(dict(doc))
            name = f"Employee-{doc['fieldname']}"
        else:
            name = doc.get("name") or "generated"
        return {"name": name, **doc}


def _full_engine():
    """`_engine()` plus the field Bank Branch gets for itself."""
    meta = make_meta("Employee", [
        make_field("first_name", "First Name", reqd=True),
        make_field("employee_name", "Full Name"),
        make_field("employee_number", "Employee Number"),
        make_field("bank_branch", "Bank Branch"),
        make_field("department", "Department", "Link", options="Department"),
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


# --------------------------------------------------------------- the full name
def test_full_name_maps_to_first_name_not_employee_name():
    """`employee_name` is recomputed from first/middle/last, so mapping it is a
    no-op that also leaves the required `first_name` empty."""
    engine = _engine()
    sheet = make_sheet(["Emp ID", "Full Name"], [["EMP-001", "Tan Wei Ming"]])
    plan = engine.suggest(sheet)
    assert _target(plan, "Full Name") == "employee_name", "the scorer's pick"

    lines = employees.apply(engine, plan, sheet, None)

    assert _target(plan, "Full Name") == "first_name"
    assert any("kept whole" in line for line in lines)


def test_the_full_name_is_not_split():
    engine = _engine()
    sheet = make_sheet(["Full Name"], [["Tan Wei Ming"]])
    plan = engine.suggest(sheet)
    employees.apply(engine, plan, sheet, None)
    m = next(m for m in plan.mappings if m.source == "Full Name")
    assert m.target == "first_name", "no last_name / middle_name column is written"


# ----------------------------------------------------------------- bank branch
def test_bank_branch_maps_to_its_own_field_once_it_exists():
    """Created with the label `Bank Branch`, so the scorer matches it exactly —
    nothing to force. Before it exists there is nothing to map to."""
    engine = _full_engine()
    sheet = make_sheet(["Emp ID", "Bank Branch"], [["EMP-001", "Jurong East"]])
    plan = engine.suggest(sheet)
    lines = employees.apply(engine, plan, sheet, None)
    assert _target(plan, "Bank Branch") == "bank_branch"
    assert not any("no Employee field yet" in line for line in lines)


def test_bank_branch_is_not_left_on_the_office_branch_link():
    """`branch` is a Link to Branch — the office location. Creating Branch
    records out of bank names is the failure this prevents."""
    engine = _engine()          # no bank_branch on the doctype yet
    engine.targets.append(type(engine.targets[0])(
        "Employee", None, None, "branch", "Branch",
        make_field("branch", "Branch", "Link", options="Branch")))
    sheet = make_sheet(["Bank Branch"], [["Jurong East"]])
    plan = engine.suggest(sheet)
    lines = employees.apply(engine, plan, sheet, None)
    assert _target(plan, "Bank Branch") == "branch", "the wrong target, until apply"
    assert any("no Employee field yet" in line and "bank_branch" in line
               for line in lines)


# ----------------------------------------------------------------- department
def _dept_sheet(rows):
    return make_sheet(["Emp ID", "Company", "Department"],
                      [list(r) for r in rows])


def test_department_targets_use_the_company_abbreviation():
    site = _Site(companies={"CP Machines": "DM"},
                 departments=[{"name": "Management - DM",
                               "department_name": "Management"}])
    sheet = _dept_sheet([("E-1", "CP Machines", "Management"),
                         ("E-2", "CP Machines", "Warehouse")])
    mapping, to_create = employees.department_plan(sheet, site)
    assert mapping == {"Management": "Management - DM",
                       "Warehouse": "Warehouse - DM"}
    assert to_create == [{"department_name": "Warehouse", "company": "CP Machines"}], (
        "only the value with no counterpart is created")


def test_an_existing_department_is_reused_by_name_not_duplicated():
    """One created outside this tool — different abbr, or renamed — wins."""
    site = _Site(companies={"CP Machines": "DM"},
                 departments=[{"name": "Management - LEGACY",
                               "department_name": "Management"}])
    mapping, to_create = employees.department_plan(
        _dept_sheet([("E-1", "CP Machines", "Management")]), site)
    assert mapping == {"Management": "Management - LEGACY"}
    assert to_create == []


def test_the_value_map_is_installed_without_clobbering_a_recorded_one():
    site = _Site(companies={"CP Machines": "DM"})
    engine, plan = _full_engine(), None
    sheet = _dept_sheet([("E-1", "CP Machines", "Management")])
    plan = engine.suggest(sheet)
    plan.value_maps["department"] = {"Management": "Hand Picked"}

    employees.apply(engine, plan, sheet, site)

    assert plan.value_maps["department"]["Management"] == "Hand Picked"


def test_two_companies_stop_the_department_mapping():
    """A value_map is keyed by value, so it cannot carry a per-row company."""
    site = _Site(companies={"CP Machines": "DM", "Other Co": "OC"})
    mapping, to_create = employees.department_plan(
        _dept_sheet([("E-1", "CP Machines", "Management"),
                     ("E-2", "Other Co", "Management")]), site)
    assert (mapping, to_create) == ({}, [])


# -------------------------------------------------------------- prerequisites
class _Journal:
    def __init__(self):
        self.fields, self.records = [], []

    def custom_field_created(self, doctype, fieldname, name, label=""):
        self.fields.append((doctype, fieldname, label))

    def record_created(self, doctype, name):
        self.records.append((doctype, name))


def test_prerequisites_create_the_field_and_the_missing_department():
    site = _Site(companies={"CP Machines": "DM"})
    sheet = make_sheet(["Bank Branch", "Company", "Department"],
                       [["Jurong East", "CP Machines", "Warehouse"]])
    journal = _Journal()

    lines = employees.prerequisites(sheet, site, journal)

    assert ("Custom Field", {"dt": "Employee", "fieldname": "bank_branch"}) in [
        (dt, {k: v for k, v in doc.items() if k in ("dt", "fieldname")})
        for dt, doc in site.inserted]
    assert journal.fields == [("Employee", "bank_branch", "Bank Branch")]
    assert journal.records == [("Department", "Warehouse - DM")]
    assert any("created Department 'Warehouse - DM'" in line for line in lines)


def test_prerequisites_are_idempotent():
    site = _Site(companies={"CP Machines": "DM"},
                 custom_fields=[{"dt": "Employee", "fieldname": "bank_branch",
                                 "label": "Bank Branch", "fieldtype": "Data"}],
                 departments=[{"name": "Warehouse - DM",
                               "department_name": "Warehouse"}])
    sheet = make_sheet(["Bank Branch", "Company", "Department"],
                       [["Jurong East", "CP Machines", "Warehouse"]])

    lines = employees.prerequisites(sheet, site, _Journal())

    assert site.inserted == [], "nothing to create the second time"
    assert any("already exists" in line for line in lines)


# ------------------------------------------------- the analysis sees the map
def test_link_checks_validate_the_mapped_value():
    """Department `Management` -> `Management - DM`: checking the raw value
    reports a conflict the import never hits."""
    from erpgen.analysis import _mapped_values
    from erpgen.mapper import MappingPlan

    plan = MappingPlan(doctype="Employee",
                       value_maps={"department": {"Management": "Management - DM"}})
    assert _mapped_values(["Management", "Warehouse"], plan, "department") == [
        "Management - DM", "Warehouse"]
    assert _mapped_values(["Management"], plan, "company") == ["Management"], (
        "another field's values are untouched")
