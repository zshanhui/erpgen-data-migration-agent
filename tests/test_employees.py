"""Employee ID mapping: the source's employee number becomes the dedup key.

ERPNext names an Employee from `naming_series` and keeps the legacy key in
`employee_number`, so the ID column does not map on its own — `Emp ID` shares no
token with the field, and `Employee ID` matches the server-derived `employee`
field instead. Getting this wrong is not cosmetic: the probe run created 48
records for 25 rows.
"""
from __future__ import annotations

import pytest

from conftest import make_field, make_meta, make_sheet
from erpgen import employees
from erpgen.client import ERPNextError
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
        make_field("relieving_date", "Relieving Date", "Date"),
        make_field("company", "Company", "Link", options="Company"),
        make_field("department", "Department", "Link", options="Department"),
        make_field("designation", "Designation", "Link", options="Designation"),
    ], autoname="naming_series:")
    return MappingEngine(meta)


class _Site:
    """Only what the employee helpers touch: `doctype_meta`, `list`, `get`,
    `insert`, `update`."""

    def __init__(self, companies=None, departments=(), custom_fields=(),
                 employee_fields=None, employees=None):
        self.companies = companies or {}
        self.departments = {d["name"]: d for d in departments}
        self.custom_fields = list(custom_fields)
        self.employees = employees or {}    # {employee_number: {name, reports_to}}
        self.employee_fields = employee_fields or [
            make_field("first_name", "First Name", reqd=True).as_dict(),
            make_field("employee_number", "Employee Number").as_dict(),
        ]
        self.inserted: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, dict]] = []

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
        if doctype == "Employee":
            wanted = None
            for f in (filters or []):
                if f[0] == "employee_number" and f[1] == "in":
                    wanted = set(f[2])
            return [{"name": doc["name"], "employee_number": number,
                     "reports_to": doc.get("reports_to", "")}
                    for number, doc in self.employees.items()
                    if wanted is None or number in wanted]
        return []

    def get(self, doctype, name):
        for number, doc in self.employees.items():
            if doc["name"] == name:
                return {"name": name, "employee_number": number,
                        "reports_to": doc.get("reports_to", "")}
        raise ERPNextError(f"HTTP 404 GET /api/resource/{doctype}/{name}")

    def update(self, doctype, name, fields):
        self.updated.append((name, dict(fields)))
        for doc in self.employees.values():
            if doc["name"] == name:
                doc.update(fields)
                return {"name": name, **doc}
        raise ERPNextError(f"HTTP 404 PUT /api/resource/{doctype}/{name}")

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
        self.fields, self.records, self.updates = [], [], []

    def custom_field_created(self, doctype, fieldname, name, label=""):
        self.fields.append((doctype, fieldname, label))

    def record_created(self, doctype, name):
        self.records.append((doctype, name))

    def record_updated(self, doctype, name, before):
        self.updates.append((doctype, name, before))

    def close(self, status="ok"):
        pass

    def summary(self):
        return "journal" 


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


# ------------------------------------------------------------- the leaving date
def test_last_working_day_maps_to_relieving_date():
    """`employee.validate_status()` throws "Please enter relieving date." for a
    Left employee without one, so the column cannot stay unmapped. Nothing
    contests the field, so a synonym entry is enough — no forcing needed."""
    engine = _engine()
    sheet = make_sheet(["Emp ID", "Employment Status", "Last Working Day"],
                       [["EMP-001", "Left", "2026-05-29"]])
    plan = engine.suggest(sheet)

    m = next(m for m in plan.mappings if m.source == "Last Working Day")

    assert m.target == "relieving_date"
    assert m.method == "synonym"
    assert not m.alternatives, "nothing else scores, so nothing to disambiguate"


def test_the_leaving_date_survives_into_the_payload():
    engine = _engine()
    sheet = make_sheet(["Emp ID", "Employment Status", "Last Working Day"],
                       [["EMP-001", "Left", "2026-05-29"],
                        ["EMP-002", "Active", ""]])
    plan = engine.suggest(sheet)
    employees.apply(engine, plan, sheet, None)
    payloads, errors = engine.build_payloads(sheet, plan)

    assert errors == []
    assert payloads[0]["relieving_date"] == "2026-05-29"
    assert "relieving_date" not in payloads[1], "empty cell stays empty"


# --------------------------------------------------------- the reporting tree
def _tree_sheet(rows, headers=("Emp ID", "Full Name", "Reporting Manager")):
    return make_sheet(list(headers), [list(r) for r in rows])


def _site_with(employees, **kw):
    """{number: (docname, manager docname)} -> a site."""
    return _Site(employees={n: {"name": name, "reports_to": reports}
                            for n, (name, reports) in employees.items()}, **kw)


def test_link_managers_sets_reports_to_from_the_sheet_names():
    site = _site_with({"EMP-001": ("HR-EMP-00001", ""),
                       "EMP-002": ("HR-EMP-00002", "")})
    sheet = _tree_sheet([("EMP-001", "Tan Wei Ming", ""),
                         ("EMP-002", "Priya Nair", "Tan Wei Ming")])

    lines, problems = employees.link_managers(site, sheet, _Journal())

    assert problems == []
    assert site.updated == [("HR-EMP-00002", {"reports_to": "HR-EMP-00001"})], (
        "the Link takes the docname the site assigned, not the sheet's Emp ID")
    assert any("linked 1" in line for line in lines)


def test_link_managers_leaves_rows_that_already_hold_the_right_manager():
    """update_record journals unconditionally, so a no-op write would put a
    junk inverse in the journal on every re-run."""
    site = _site_with({"EMP-001": ("HR-EMP-00001", ""),
                       "EMP-002": ("HR-EMP-00002", "HR-EMP-00001")})
    sheet = _tree_sheet([("EMP-001", "Tan Wei Ming", ""),
                         ("EMP-002", "Priya Nair", "Tan Wei Ming")])

    lines, problems = employees.link_managers(site, sheet, _Journal())

    assert site.updated == []
    assert problems == []
    assert any("already had" in line for line in lines)


def test_link_managers_links_what_it_can_and_reports_the_rest():
    site = _site_with({"EMP-002": ("HR-EMP-00002", "")})
    sheet = _tree_sheet([("EMP-002", "Priya Nair", "Nobody At All")])

    lines, problems = employees.link_managers(site, sheet, _Journal())

    assert lines == []
    assert site.updated == []
    assert len(problems) == 1 and "Nobody At All" in problems[0]
    assert "row 2" in problems[0]


def test_link_managers_skips_a_self_report():
    site = _site_with({"EMP-001": ("HR-EMP-00001", "")})
    sheet = _tree_sheet([("EMP-001", "Tan Wei Ming", "Tan Wei Ming")])

    lines, problems = employees.link_managers(site, sheet, _Journal())

    assert site.updated == [], "ERPNext throws on a self-report"
    assert len(problems) == 1 and "own report" in problems[0]


def test_link_managers_reports_a_manager_that_is_not_on_the_site():
    site = _site_with({"EMP-002": ("HR-EMP-00002", "")})   # EMP-001 missing
    sheet = _tree_sheet([("EMP-001", "Tan Wei Ming", ""),
                         ("EMP-002", "Priya Nair", "Tan Wei Ming")])

    _lines, problems = employees.link_managers(site, sheet, _Journal())

    assert site.updated == []
    assert len(problems) == 1 and "EMP-001" in problems[0]


def test_link_managers_accepts_an_employee_id_in_the_manager_column():
    site = _site_with({"EMP-001": ("HR-EMP-00001", ""),
                       "EMP-002": ("HR-EMP-00002", "")})
    sheet = _tree_sheet([("EMP-001", "Tan Wei Ming", ""),
                         ("EMP-002", "Priya Nair", "EMP-001")])

    _lines, problems = employees.link_managers(site, sheet, _Journal())

    assert problems == []
    assert site.updated == [("HR-EMP-00002", {"reports_to": "HR-EMP-00001"})]


def test_link_managers_journals_the_previous_manager_for_revert():
    journal = _Journal()
    site = _site_with({"EMP-001": ("HR-EMP-00001", ""),
                       "EMP-002": ("HR-EMP-00002", "HR-EMP-00009")})
    sheet = _tree_sheet([("EMP-001", "Tan Wei Ming", ""),
                         ("EMP-002", "Priya Nair", "Tan Wei Ming")])

    employees.link_managers(site, sheet, journal)

    assert journal.updates == [("Employee", "HR-EMP-00002",
                                {"reports_to": "HR-EMP-00009"})]


def test_link_managers_is_a_no_op_without_a_manager_column():
    site = _site_with({})
    sheet = make_sheet(["Emp ID", "Full Name"], [["EMP-001", "Tan Wei Ming"]])
    assert employees.link_managers(site, sheet, _Journal()) == ([], [])


# ------------------------------------------------------ the agent can reach it
def test_the_agent_has_a_link_managers_tool():
    from erpgen.agent.tools import TOOLS
    assert "link_managers" in {t["name"] for t in TOOLS}


def test_the_tool_forwards_the_run_id(monkeypatch):
    """Without --run the tree updates journal to their own file, and
    `revert <run-id>` would undo the employees but leave the hierarchy."""
    from erpgen.agent import tools as agent_tools

    seen: list[list] = []
    monkeypatch.setattr(agent_tools, "_erpgen",
                        lambda args, timeout=0: (seen.append(list(args)), (0, "ok"))[1])
    agent_tools._TRANSCRIPT_CTX["run"] = "hr-01"
    try:
        out = agent_tools.t_link_managers("samples/employees.csv")
    finally:
        agent_tools._TRANSCRIPT_CTX.pop("run", None)

    assert seen == [["--run", "hr-01", "link-managers", "samples/employees.csv",
                     "--doctype", "Employee"]]
    assert "link-managers exit 0" in out


# ------------------------------------------- the flag on the import (one command)
class _Sink:
    def close(self):
        pass


class _Logger:
    def run_end(self, **kw):
        pass

    def summary(self, doctype, source):
        return f"summary for {doctype}"


def _import_args(cli, source, *extra):
    return cli.build_parser().parse_args(
        ["import", str(source), "--doctype", "Employee", "--apply", *extra])


@pytest.mark.parametrize("flag,expected", [(["--link-managers"], ["load", "link"]),
                                           ([], ["load"])])
def test_the_link_managers_flag_runs_the_second_pass_after_the_rows(
        cli, monkeypatch, tmp_path, flag, expected):
    """`import --apply --link-managers` is the whole migration in one command.
    The order matters: reports_to can only be set once the records exist."""
    source = tmp_path / "employees.csv"
    source.write_text("Emp ID,Full Name,Reporting Manager\n"
                      "EMP-001,Tan Wei Ming,\n"
                      "EMP-002,Priya Nair,Tan Wei Ming\n", encoding="utf-8")
    engine, client, order = _engine(), object(), []

    monkeypatch.setattr(cli, "_client", lambda args: client)
    monkeypatch.setattr(cli, "_engine", lambda *a, **kw: (engine, client))
    monkeypatch.setattr(cli, "_overrides_for", lambda *a, **kw: (None, {}))
    monkeypatch.setattr(cli, "_load_corrections", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_effect_sink", lambda *a, **kw: _Sink())
    monkeypatch.setattr(cli.employees, "prerequisites", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "build_analysis", lambda *a, **kw: {
        "conflicts": [], "suggested_custom_fields": []})
    monkeypatch.setattr(cli, "save_analysis", lambda *a, **kw: str(tmp_path / "a.json"))
    monkeypatch.setattr(cli, "_open_import_run",
                        lambda *a, **kw: (_Logger(), _Journal(), None))
    monkeypatch.setattr(cli, "_predicted_dedup", lambda *a, **kw: ([], set()))
    monkeypatch.setattr(cli, "dedup_payloads", lambda *a, **kw: ([], []))
    monkeypatch.setattr(cli, "_load_payloads", lambda *a, **kw: order.append("load"))
    monkeypatch.setattr(cli, "_verified_created", lambda *a, **kw: 0)

    def _link(*a, **kw):
        order.append("link")
        return ["linked 1 employee(s) to their reporting manager"], []

    monkeypatch.setattr(cli.employees, "link_managers", _link)

    assert cli.cmd_import(_import_args(cli, source, *flag)) == 0
    assert order == expected


def test_the_link_managers_flag_is_ignored_for_other_doctypes(cli, monkeypatch,
                                                              tmp_path):
    """A Customer sheet with a 'Manager' column must not start linking Employees."""
    source = tmp_path / "customers.csv"
    source.write_text("Customer Name,Manager\nAcme,Tan Wei Ming\n", encoding="utf-8")
    engine = MappingEngine(make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True)], ))
    order: list[str] = []

    monkeypatch.setattr(cli, "_client", lambda args: object())
    monkeypatch.setattr(cli, "_engine", lambda *a, **kw: (engine, object()))
    monkeypatch.setattr(cli, "_overrides_for", lambda *a, **kw: (None, {}))
    monkeypatch.setattr(cli, "_load_corrections", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_effect_sink", lambda *a, **kw: _Sink())
    monkeypatch.setattr(cli.employees, "prerequisites", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "build_analysis", lambda *a, **kw: {
        "conflicts": [], "suggested_custom_fields": []})
    monkeypatch.setattr(cli, "save_analysis", lambda *a, **kw: str(tmp_path / "a.json"))
    monkeypatch.setattr(cli, "_open_import_run",
                        lambda *a, **kw: (_Logger(), _Journal(), None))
    monkeypatch.setattr(cli, "_predicted_dedup", lambda *a, **kw: ([], set()))
    monkeypatch.setattr(cli, "dedup_payloads", lambda *a, **kw: ([], []))
    monkeypatch.setattr(cli, "_load_payloads", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_verified_created", lambda *a, **kw: 0)
    monkeypatch.setattr(cli.employees, "link_managers",
                        lambda *a, **kw: (order.append("link"), ([], []))[1])

    args = cli.build_parser().parse_args([
        "import", str(source), "--doctype", "Customer", "--apply", "--link-managers"])
    assert cli.cmd_import(args) == 0
    assert order == []
