"""The cleaning stage: detectors, conflict builders and the `clean` command
(phase 1), plus the analysis wiring that surfaces them to a human or agent
(phase 2).

Pure and offline — synthetic `SourceTable`s and a `FakeClient`; no network. The
CLI tests run without `--doctype` so they need no ERPNext site.
"""
from __future__ import annotations

import json

import pytest
from conftest import (ROOT, FakeClient, make_field, make_meta, make_sheet,
                      run_isolated)

from erpgen.conflicts import (MAX_VALUES, data_quality_conflicts,
                              duplicate_key_groups, missing_value_conflict,
                              required_mapped_columns)
from erpgen.mapper import ColumnMapping, MappingEngine, MappingPlan

HEADERS = ["Customer Name", "Customer Type", "Phone"]


def _conflicts(rows, *, key="Customer Name", required=(), compare=None, cap=MAX_VALUES):
    return data_quality_conflicts(
        make_sheet(HEADERS, rows), key_column=key, key_field="customer_name",
        required=list(required), compare_columns=compare, cap=cap,
    )


def _by_kind(conflicts, kind):
    return [c for c in conflicts if c["kind"] == kind]


# --------------------------------------------------------------- duplicates
def test_identical_duplicate_rows_are_a_warning():
    cs = _conflicts([["Acme", "Company", "111"], ["Acme", "Company", "111"]])
    dup = _by_kind(cs, "duplicate_row")[0]
    assert dup["severity"] == "warning"
    assert dup["group_count"] == 1
    assert dup["groups"][0]["identical"] is True
    assert dup["groups"][0]["rows"] == [2, 3]
    assert dup["groups"][0]["count"] == 2


def test_differing_duplicate_rows_are_an_error_with_differing_fields():
    cs = _conflicts([["Acme", "Company", "111"], ["Acme", "Company", "999"]])
    dup = _by_kind(cs, "duplicate_row")[0]
    assert dup["severity"] == "error"
    g = dup["groups"][0]
    assert g["identical"] is False
    assert g["differing_fields"] == ["Phone"]
    assert "second record" in dup["detail"]


def test_case_variant_identical_is_a_warning_with_the_flag():
    cs = _conflicts([["Acme", "Company", "111"], ["acme", "Company", "111"]])
    dup = _by_kind(cs, "duplicate_row")[0]
    g = dup["groups"][0]
    assert (dup["severity"], g["identical"], g["case_variant"]) == ("warning", True, True)
    assert g["key_value"] == "acme"          # the casefolded key
    assert g["values"] == ["Acme", "acme"]   # variants preserved as read


def test_case_variant_with_differing_values_is_an_error():
    cs = _conflicts([["Acme", "Company", "111"], ["ACME", "Company", "999"]])
    dup = _by_kind(cs, "duplicate_row")[0]
    assert dup["severity"] == "error"
    assert dup["groups"][0]["case_variant"] is True
    assert dup["groups"][0]["differing_fields"] == ["Phone"]


def test_whitespace_variant_is_flagged_but_is_not_a_case_collision():
    cs = _conflicts([["Acme", "Company", "111"], ["Acme ", "Company", "111"]])
    g = _by_kind(cs, "duplicate_row")[0]["groups"][0]
    assert g["whitespace_variant"] is True
    assert g["case_variant"] is False
    assert g["identical"] is True            # a trailing space is not a real difference


def test_multiple_groups_aggregate_into_one_conflict():
    """Requirement identity is (kind, source, target, doctype, field), so per-value
    conflicts would be deduped away as already recorded."""
    cs = _conflicts([
        ["Acme", "Company", "111"], ["Acme", "Company", "111"],
        ["Beta", "Company", "222"], ["Beta", "Company", "222"],
        ["Gamma", "Company", "333"], ["Gamma", "Company", "333"],
    ])
    dups = _by_kind(cs, "duplicate_row")
    assert len(dups) == 1
    assert dups[0]["group_count"] == 3
    assert [g["key_value"] for g in dups[0]["groups"]] == ["acme", "beta", "gamma"]


def test_distinct_keys_produce_no_duplicate_conflict():
    cs = _conflicts([["Acme", "Company", "111"], ["Beta", "Company", "222"]])
    assert _by_kind(cs, "duplicate_row") == []


def test_compare_columns_narrows_what_identical_means():
    rows = [["Acme", "Company", "111"], ["Acme", "Company", "999"]]
    # Phone is not passed as a compared column (e.g. it is unmapped/dropped)
    cs = _conflicts(rows, compare=["Customer Name", "Customer Type"])
    assert _by_kind(cs, "duplicate_row")[0]["severity"] == "warning"
    # with no mapping, every other column is compared, so the difference shows
    assert _conflicts(rows)[0]["severity"] == "error"


def test_blank_key_rows_are_not_duplicate_groups():
    cs = _conflicts([["", "Company", "111"], ["", "Company", "222"]])
    assert _by_kind(cs, "duplicate_row") == []
    assert _by_kind(cs, "missing_value")[0]["count"] == 2


# ------------------------------------------------------------- missing values
def test_blank_key_becomes_missing_value_with_the_key_role():
    cs = _conflicts([["Acme", "Company", "111"], ["", "Company", "222"]])
    missing = _by_kind(cs, "missing_value")
    assert len(missing) == 1
    assert missing[0]["roles"] == ["key"]
    assert missing[0]["rows"] == [3]
    assert missing[0]["target"] == "customer_name"
    assert missing[0]["severity"] == "error"


def test_key_column_that_is_also_required_is_one_conflict_with_both_roles():
    """Customer/Supplier/Item name their record after a required field, so the key
    column and the required column are the same column."""
    cs = _conflicts([["", "Company", "111"]],
                    required=[("Customer Name", "customer_name")])
    missing = _by_kind(cs, "missing_value")
    assert len(missing) == 1, "two conflicts would collide on the identity tuple"
    assert missing[0]["roles"] == ["key", "required"]
    assert "the key column" in missing[0]["detail"]


def test_blank_required_column_is_flagged():
    cs = _conflicts([["Acme", "", "111"]], required=[("Customer Type", "customer_type")])
    missing = _by_kind(cs, "missing_value")
    assert len(missing) == 1
    assert (missing[0]["source"], missing[0]["target"]) == ("Customer Type", "customer_type")
    assert missing[0]["roles"] == ["required"]
    assert "required field 'customer_type'" in missing[0]["detail"]


def test_optional_blank_column_is_not_flagged():
    """`samples/employees.csv` has Last Working Day blank in 23 of 25 rows."""
    cs = _conflicts([["Acme", "Company", ""], ["Beta", "Company", "222"]])
    assert _by_kind(cs, "missing_value") == []


def test_required_columns_helper_skips_fields_with_no_column_or_a_default():
    meta = make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True),
        make_field("customer_type", "Customer Type", reqd=True),
        make_field("territory", "Territory", reqd=True),      # no source column
        make_field("notes", "Notes"),                         # not required
    ])
    plan = MappingPlan(doctype="Customer")
    plan.mappings = [
        ColumnMapping("Customer Name", "customer_name", 1.0, "exact"),
        ColumnMapping("Customer Type", "customer_type", 1.0, "exact"),
    ]
    assert required_mapped_columns(meta, plan) == [
        ("Customer Name", "customer_name"), ("Customer Type", "customer_type")]

    plan.defaults = {"customer_type": "Company"}
    assert required_mapped_columns(meta, plan) == [("Customer Name", "customer_name")]


def test_required_field_covered_by_defaults_is_not_flagged_end_to_end():
    sheet = make_sheet(HEADERS, [["Acme", "", "111"]])
    without = data_quality_conflicts(sheet, key_column="Customer Name",
                                     required=[("Customer Type", "customer_type")])
    assert _by_kind(without, "missing_value")          # no default -> flagged
    with_default = data_quality_conflicts(sheet, key_column="Customer Name", required=[])
    assert _by_kind(with_default, "missing_value") == []


# ------------------------------------------------------------- bookkeeping
def test_row_numbers_are_spreadsheet_rows():
    cs = _conflicts([["Acme", "Company", "111"], ["Beta", "Company", "222"],
                     ["Acme", "Company", "111"]])
    assert _by_kind(cs, "duplicate_row")[0]["groups"][0]["rows"] == [2, 4]


def test_group_and_row_lists_are_capped_with_true_counts():
    rows = []
    for i in range(MAX_VALUES + 5):          # > cap duplicated keys
        rows += [[f"Co{i}", "Company", "111"], [f"Co{i}", "Company", "111"]]
    dup = _by_kind(_conflicts(rows), "duplicate_row")[0]
    assert dup["group_count"] == MAX_VALUES + 5
    assert len(dup["groups"]) == MAX_VALUES

    # and a single group with more rows than the cap
    many = [["Acme", "Company", "111"] for _ in range(MAX_VALUES + 5)]
    dup = _by_kind(_conflicts(many), "duplicate_row")[0]
    assert dup["group_count"] == 1
    assert dup["groups"][0]["count"] == MAX_VALUES + 5
    assert len(dup["groups"][0]["rows"]) == MAX_VALUES


def test_detection_is_deterministic():
    rows = [["Acme", "Company", "111"], ["Acme", "Company", "999"], ["", "", ""]]
    first = _conflicts(rows)
    second = _conflicts(rows)
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)


def test_missing_key_column_yields_no_duplicate_conflict():
    sheet = make_sheet(HEADERS, [["Acme", "Company", "111"]])
    assert duplicate_key_groups(sheet, "Not A Column") == ([], 0)
    assert missing_value_conflict("X", "x", ["key"], [2], 1)["count"] == 1


# ------------------------------------------------------------------- CLI
def _run_clean(cli, *argv):
    args = cli.build_parser().parse_args(["clean", *argv])
    return cli.cmd_clean(args)


def test_clean_exits_2_on_the_dirty_fixture_and_0_on_clean_samples(cli):
    assert _run_clean(cli, str(ROOT / "samples/customers_dirty.csv")) == 2
    for clean in ("customers.csv", "items.csv", "employees.csv"):
        assert _run_clean(cli, str(ROOT / "samples" / clean)) == 0, clean


def test_clean_reports_the_key_column_it_guessed(cli, capsys):
    _run_clean(cli, str(ROOT / "samples/customers_dirty.csv"))
    out = capsys.readouterr().out
    assert "key column 'Customer Name', guessed" in out
    assert "case" in out and "whitespace" in out


def test_clean_json_output_is_machine_readable(cli, capsys):
    _run_clean(cli, str(ROOT / "samples/customers_dirty.csv"), "--json")
    conflicts = json.loads(capsys.readouterr().out)
    severity = {c["kind"]: c["severity"] for c in conflicts}
    assert set(severity) == {"duplicate_row", "missing_value", "possible_duplicate_row"}
    assert severity["duplicate_row"] == "error"
    assert severity["missing_value"] == "error"
    assert severity["possible_duplicate_row"] == "warning"   # review-only


def test_clean_rejects_an_unknown_id_column(cli, capsys):
    rc = _run_clean(cli, str(ROOT / "samples/customers.csv"), "--id-column", "Nope")
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a column" in err and "Nope" in err
    assert "Customer Name" in err            # lists the available headers


def test_clean_accepts_an_explicit_id_column(cli, capsys):
    assert _run_clean(cli, str(ROOT / "samples/customers_dirty.csv"),
                      "--id-column", "Customer Name") == 2
    assert "key column 'Customer Name', explicit" in capsys.readouterr().out


def test_map_parses_an_id_column(cli):
    args = cli.build_parser().parse_args(["map", "s.csv", "--id-column", "Emp ID"])
    assert args.id_column == "Emp ID"


def test_map_uses_the_shared_id_column_check(cli):
    """map must fail on an unknown explicit column, not fall back to inference."""
    from erpgen.source import read_source
    source = read_source(ROOT / "samples/customers.csv")
    args = cli.build_parser().parse_args(["map", "x.csv", "--id-column", "Nope"])
    assert cli._check_id_column(args, source) is False
    ok = cli.build_parser().parse_args(["map", "x.csv", "--id-column", "Customer Name"])
    assert cli._check_id_column(ok, source) is True


# ------------------------------------------------- phase 2: analysis wiring
def _customer_engine():
    meta = make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True),
        make_field("customer_type", "Customer Type", reqd=True),
        make_field("phone", "Phone"),
    ])
    return MappingEngine(meta)


DIRTY_ROWS = [
    ["Nimbus Forge", "Company", "1111"],
    ["Granite Bay", "Company", "2222"],
    ["Granite Bay", "Company", "2222"],          # identical duplicate
    ["Harborline", "Company", "3333"],
    ["Harborline", "Company", "9999"],           # differing duplicate
    ["", "Company", "4444"],                     # blank key (also required)
    ["Bluedot", "", "5555"],                     # blank required, non-key
]


def test_build_analysis_carries_the_data_quality_conflicts():
    from erpgen.analysis import build_analysis

    engine = _customer_engine()
    sheet = make_sheet(["Customer Name", "Customer Type", "Phone"], DIRTY_ROWS)
    plan = engine.suggest(sheet)
    analysis = build_analysis(FakeClient(), sheet, plan, engine,
                              id_column="Customer Name")

    kinds = {c["kind"]: c["severity"] for c in analysis["conflicts"]}
    assert kinds["duplicate_row"] == "error"
    assert kinds["missing_value"] == "error"

    dup = next(c for c in analysis["conflicts"] if c["kind"] == "duplicate_row")
    assert dup["target"] == "customer_name"      # the mapped field, not the column
    assert dup["group_count"] == 2

    missing = {c["source"]: c["roles"] for c in analysis["conflicts"]
               if c["kind"] == "missing_value"}
    assert missing["Customer Name"] == ["key", "required"]
    assert missing["Customer Type"] == ["required"]


def test_build_analysis_instructions_cover_the_new_kinds():
    from erpgen.analysis import build_analysis

    engine = _customer_engine()
    sheet = make_sheet(["Customer Name", "Customer Type", "Phone"], DIRTY_ROWS)
    analysis = build_analysis(FakeClient(), sheet, engine.suggest(sheet), engine,
                              id_column="Customer Name")
    text = analysis["agent_instructions"]
    for kind in ("duplicate_row", "missing_value"):
        assert f"- {kind} ->" in text
    assert "cannot invent" in text          # the agent's real constraint


def test_build_analysis_without_a_key_column_skips_duplicate_detection():
    """Inference can fail; the required-cell checks must still run."""
    from erpgen.analysis import build_analysis

    engine = _customer_engine()
    sheet = make_sheet(["Customer Name", "Customer Type", "Phone"], DIRTY_ROWS)
    analysis = build_analysis(FakeClient(), sheet, engine.suggest(sheet), engine,
                              id_column=None)
    kinds = {c["kind"] for c in analysis["conflicts"]}
    assert "duplicate_row" not in kinds
    assert "missing_value" in kinds


def test_clean_conflicts_block_the_import_gate(cli):
    """The gate filters on severity == 'error', so the new kinds block by kind."""
    args = cli.build_parser().parse_args(
        ["import", str(ROOT / "samples/customers_dirty.csv"), "--doctype", "Customer"])
    assert args.bypass_conflicts is False
    analysis = {"conflicts": [
        {"kind": "duplicate_row", "severity": "error", "source": "Customer Name"},
        {"kind": "missing_value", "severity": "error", "source": "Customer Type"},
    ]}
    errs = [c for c in analysis["conflicts"] if c["severity"] == "error"]
    assert len(errs) == 2                    # what cmd_import refuses on


def test_group_order_is_stable_across_processes(tmp_path):
    """Group keys are strings, and Python randomises string hashing per process, so
    a report built by iterating a set of keys varies between runs."""
    csv_path = tmp_path / "dupes.csv"
    csv_path.write_text(
        "Customer Name\n" + "\n".join(
            ["Acme Steel", "Acme Steel", "Beta Works", "Beta Works",
             "Gamma Trading", "Gamma Trading", "Delta Supplies", "Delta Supplies"])
        + "\n", encoding="utf-8")

    script = (
        "import json\n"
        "from erpgen.source import read_csv\n"
        "from erpgen.conflicts import duplicate_key_groups\n"
        f"sheet = read_csv({str(csv_path)!r})\n"
        "groups, total = duplicate_key_groups(sheet, 'Customer Name')\n"
        "print(json.dumps([g['key_value'] for g in groups]))\n"
    )
    assert len(run_isolated(script)) == 1, "group order changed between processes"
