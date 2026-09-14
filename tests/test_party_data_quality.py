"""Phase 3: the flat party-sheet analyser surfaces the same cleaning conflicts.

The point of this file is the **parity test**: `conflicts.py` exists so the two
analysers "cannot drift apart", and both now call the same detectors. One injected
defect must produce the same conflict shape from either entry point.

Pure: a duck-typed in-memory client, no network. The stub serves only what
`DoctypeMeta.fetch` and `fetch_with_children` read.
"""
from __future__ import annotations

from conftest import make_sheet

from erpgen.analysis import build_analysis
from erpgen.customers_full import build_party_sheet_analysis, flat_map_for, spec_for
from erpgen.mapper import MappingEngine
from erpgen.metadata import DoctypeMeta

# --------------------------------------------------------------- metadata stub
def _f(fieldname, label, fieldtype="Data", **kw):
    return {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, **kw}


def _meta(name, fields):
    return {"name": name, "autoname": None, "istable": 0, "is_submittable": 0,
            "fields": fields}


META = {
    "Customer": _meta("Customer", [
        _f("customer_name", "Customer Name", reqd=1),
        _f("customer_type", "Customer Type", reqd=1),
        _f("customer_group", "Customer Group", "Link", options="Customer Group"),
        _f("territory", "Territory", "Link", options="Territory"),
    ]),
    # Contact has no required fields, so the flat flow must not invent any
    "Contact": _meta("Contact", [
        _f("first_name", "First Name"),
        _f("email_id", "Email Address"),
    ]),
    "Address": _meta("Address", [
        _f("address_type", "Address Type", "Select", reqd=1),
        _f("address_line1", "Address Line 1", reqd=1),
        _f("city", "City", reqd=1),
        _f("country", "Country", "Link", options="Country", reqd=1),
    ]),
}


#: Link targets the flat contract validates against. Without these every value
#: looks missing and the gate refuses even a clean sheet.
LINK_RECORDS = {
    "Customer Group": ["Commercial", "Wholesale"],
    "Territory": ["All Territories"],
    "Country": ["Singapore"],
}


class FakeSite:
    """Only the reads the analysers make."""

    def doctype_meta(self, dt):
        return META[dt]

    def list(self, doctype, filters=None, fields=None, limit=0, order_by=None):
        return [{"name": n} for n in LINK_RECORDS.get(doctype, [])]

    def get(self, dt, name):
        raise AssertionError("unexpected get")


# ------------------------------------------------------------------- fixtures
FLAT_HEADERS = ["Customer Name", "Customer Type", "Group", "Territory",
                "Contact Name", "Email", "Address Type",
                "Address Line 1", "City", "Country"]
RELATIONAL_HEADERS = ["Customer Name", "Customer Type", "Group", "Territory"]

#: one identical duplicate pair, one blank key (which is also a required field)
DEFECT = [
    ["Acme Steel", "Company", "Commercial", "All Territories"],
    ["Acme Steel", "Company", "Commercial", "All Territories"],
    ["", "Company", "Commercial", "All Territories"],
]


#: the same key twice with *differing* values (error-severity) plus a blank key
DIRTY_DEFECT = [
    ["Acme Steel", "Company", "Commercial", "All Territories"],
    ["Acme Steel", "Company", "Wholesale", "All Territories"],
    ["", "Company", "Commercial", "All Territories"],
]


def _flat_rows(defect):
    return [r + ["Alicia Ho", "a@acme.example", "Billing", "1 Foundry Way",
                 "Singapore", "Singapore"] for r in defect]


def _flat_sheet():
    return make_sheet(FLAT_HEADERS, _flat_rows(DEFECT))


def _dirty_sheet():
    """Duplicates that differ, so `duplicate_row` is error-severity and gated."""
    return make_sheet(FLAT_HEADERS, _flat_rows(DIRTY_DEFECT))


def _relational_sheet():
    return make_sheet(RELATIONAL_HEADERS, [list(r) for r in DEFECT])


def _kind(analysis, kind, source=None):
    return [c for c in analysis["conflicts"]
            if c["kind"] == kind and (source is None or c["source"] == source)]


# ---------------------------------------------------------------- parity
def test_both_analysers_report_the_same_defect_identically():
    """One defect, two entry points, one conflict shape — the anti-drift guard."""
    flat = build_party_sheet_analysis(FakeSite(), _flat_sheet(), "Customer")

    engine = MappingEngine(DoctypeMeta.from_api(META["Customer"]))
    plan = engine.suggest(_relational_sheet())
    relational = build_analysis(FakeSite(), _relational_sheet(), plan, engine,
                                id_column="Customer Name")

    for kind in ("duplicate_row", "missing_value"):
        f, r = _kind(flat, kind, "Customer Name"), _kind(relational, kind, "Customer Name")
        assert len(f) == len(r) == 1, (kind, len(f), len(r))
        assert f[0] == r[0], f"{kind} differs between analysers:\n{f[0]}\n{r[0]}"


def test_flat_analysis_keys_on_the_party_name_column():
    spec = spec_for("Customer")
    assert spec["name_column"] == "Customer Name"
    assert flat_map_for("Customer")[spec["name_column"]] == ("customer",
                                                             spec["name_field"])
    flat = build_party_sheet_analysis(FakeSite(), _flat_sheet(), "Customer")
    for c in flat["conflicts"]:
        if c["kind"] in ("duplicate_row", "missing_value"):
            assert c["source"] == "Customer Name" or c["source"] in FLAT_HEADERS


# --------------------------------------------------- per-target required cells
def test_blank_required_cells_are_checked_per_target_doctype():
    """A flat row feeds Customer + Contact + Address; Address has its own required
    fields and they must be flagged even though Customer's are filled."""
    sheet = make_sheet(FLAT_HEADERS, [
        ["Acme Steel", "Company", "Commercial", "All Territories",
         "Alicia Ho", "a@acme.example", "Billing", "", "", "Singapore"],
    ])
    flat = build_party_sheet_analysis(FakeSite(), sheet, "Customer")
    missing = {c["source"]: (c["target"], c["roles"]) for c in _kind(flat, "missing_value")}
    assert missing["Address Line 1"] == ("address_line1", ["required"])
    assert missing["City"] == ("city", ["required"])
    assert "Country" not in missing            # filled, so not flagged
    assert "Customer Name" not in missing      # filled, so not flagged


def test_contact_without_required_fields_is_never_flagged():
    sheet = make_sheet(FLAT_HEADERS, [
        ["Acme Steel", "Company", "Commercial", "All Territories",
         "", "", "Billing", "1 Foundry Way", "Singapore", "Singapore"],
    ])
    flat = build_party_sheet_analysis(FakeSite(), sheet, "Customer")
    assert not [c for c in _kind(flat, "missing_value") if c["source"] == "Contact Name"]


def test_blank_party_name_merges_both_roles():
    sheet = make_sheet(FLAT_HEADERS, [
        ["", "Company", "Commercial", "All Territories", "Alicia Ho",
         "a@acme.example", "Billing", "1 Foundry Way", "Singapore", "Singapore"],
    ])
    flat = build_party_sheet_analysis(FakeSite(), sheet, "Customer")
    (missing,) = _kind(flat, "missing_value", "Customer Name")
    assert missing["roles"] == ["key", "required"]     # one conflict, not two
    assert missing["rows"] == [2]


def test_duplicate_party_rows_are_reported_with_row_numbers():
    flat = build_party_sheet_analysis(FakeSite(), _flat_sheet(), "Customer")
    (dup,) = _kind(flat, "duplicate_row")
    assert dup["severity"] == "warning"        # identical rows: a safe skip
    assert dup["groups"][0]["rows"] == [2, 3]
    assert dup["groups"][0]["identical"] is True
    assert dup["group_count"] == 1


def test_differing_duplicate_rows_are_an_error():
    sheet = make_sheet(FLAT_HEADERS, [
        ["Acme Steel", "Company", "Commercial", "All Territories", "A", "",
         "Billing", "1 Foundry Way", "Singapore", "Singapore"],
        ["Acme Steel", "Company", "Wholesale", "All Territories", "B", "",
         "Billing", "1 Foundry Way", "Singapore", "Singapore"],
    ])
    flat = build_party_sheet_analysis(FakeSite(), sheet, "Customer")
    (dup,) = _kind(flat, "duplicate_row")
    assert dup["severity"] == "error"
    assert dup["groups"][0]["differing_fields"] == ["Group", "Contact Name"]


def test_clean_flat_sheet_reports_no_data_quality_conflicts():
    sheet = make_sheet(FLAT_HEADERS, [
        ["Acme Steel", "Company", "Commercial", "All Territories", "Alicia Ho",
         "a@acme.example", "Billing", "1 Foundry Way", "Singapore", "Singapore"],
        ["Beta Works", "Company", "Wholesale", "All Territories", "Bo Lin",
         "b@beta.example", "Shipping", "2 Depot Road", "Singapore", "Singapore"],
    ])
    flat = build_party_sheet_analysis(FakeSite(), sheet, "Customer")
    assert not _kind(flat, "duplicate_row")
    assert not _kind(flat, "missing_value")


def test_flat_instructions_cover_the_new_kinds():
    flat = build_party_sheet_analysis(FakeSite(), _flat_sheet(), "Customer")
    text = flat["agent_instructions"]
    for kind in ("duplicate_row", "missing_value"):
        assert f"- {kind} ->" in text
    assert "cannot be invented" in text


# ------------------------------------------------- flat import conflict gate
def _gate_args(cli, tmp_path, *extra):
    # --log-dir is a global flag, so it precedes the subcommand
    return cli.build_parser().parse_args([
        "--log-dir", str(tmp_path / "logs"),
        "import", "samples/x.csv", "--apply",
        "--analysis-dir", str(tmp_path / "analysis"), *extra])


def _stub_import(cli, monkeypatch):
    """Replace the client and the importer so the gate can be tested offline."""
    calls: list = []
    monkeypatch.setattr(cli, "_client", lambda args: FakeSite())
    monkeypatch.setattr(cli, "run_flat_parties_import",
                        lambda *a, **kw: calls.append(kw))
    return calls


def test_flat_import_refuses_while_error_conflicts_remain(cli, monkeypatch, tmp_path, capsys):
    calls = _stub_import(cli, monkeypatch)
    rc = cli._import_flat_party_sheet(_gate_args(cli, tmp_path), _dirty_sheet(), "Customer")

    assert rc == 2
    assert calls == [], "nothing may be imported while errors remain"
    out = capsys.readouterr().out
    assert "error-severity conflict(s) remain" in out
    assert "[duplicate_row] Customer Name" in out          # differing duplicates
    assert "[missing_value] Customer Name" in out          # blank key
    assert "or pass --bypass-conflicts to import anyway" in out


def test_flat_import_proceeds_with_bypass_conflicts(cli, monkeypatch, tmp_path):
    calls = _stub_import(cli, monkeypatch)
    rc = cli._import_flat_party_sheet(
        _gate_args(cli, tmp_path, "--bypass-conflicts"), _dirty_sheet(), "Customer")

    assert rc == 0
    assert len(calls) == 1, "the escape hatch must let the import through"


def test_flat_import_passes_when_the_sheet_is_clean(cli, monkeypatch, tmp_path):
    calls = _stub_import(cli, monkeypatch)
    sheet = make_sheet(FLAT_HEADERS, [
        ["Acme Steel", "Company", "Commercial", "All Territories", "Alicia Ho",
         "a@acme.example", "Billing", "1 Foundry Way", "Singapore", "Singapore"],
    ])
    assert cli._import_flat_party_sheet(_gate_args(cli, tmp_path), sheet, "Customer") == 0
    assert len(calls) == 1
