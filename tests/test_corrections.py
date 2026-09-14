"""Phase 5a: worksheet corrections — validation, verification, application.

The contract is `docs/gen/worksheet_schema.md`. One property shapes every test
here: the detectors always read the **raw** source, so a correction can never be
confirmed by re-detection — it is *verified* instead. A correction whose row
reference no longer resolves, or whose cell no longer holds the value it
replaced, is inert; inert blocks the gate rather than passing silently.

Row coordinates are the ones detection, the logs and `{"row": N}` all speak: the
**source line number, 1 = header**.
"""
from __future__ import annotations

import json

import pytest
from conftest import FakeClient, make_field, make_meta, make_sheet

from erpgen import corrections as C
from erpgen.customers_full import build_payloads as flat_payloads
from erpgen.customers_full import import_flat_parties
from erpgen.mapper import MappingEngine

HEADERS = ["Customer Name", "Customer Type", "Phone"]
ROWS = [
    ["Nimbus Forge", "Company", "1111"],   # line 2
    ["Granite Bay", "Company", "2222"],    # line 3
    ["Harborline", "Company", "3333"],     # line 4
    ["", "Company", ""],                   # line 5 — blank key and blank phone
    ["Bluedot", "", "5555"],               # line 6
]
KEYS = [row[0] for row in ROWS]
ALL_LINES = [2, 3, 4, 5, 6]


def _set(at, value, column="Phone", **extra):
    return {"action": "set_value", "at": at, "column": column, "value": value,
            **extra}


def _ws(corrections, key_column="Customer Name"):
    return {"source": {"key_column": key_column}, "corrections": corrections}


def _prep(corrections, rows=None, headers=None, key_column="Customer Name", **kw):
    """`prepare` over the fixture sheet; returns the Prepared and the original."""
    sheet = make_sheet(headers or HEADERS, rows if rows is not None else ROWS)
    return C.prepare(sheet, _ws(corrections, key_column), **kw), sheet


def _after(corrections, **kw):
    prepared, _ = _prep(corrections, **kw)
    return prepared


def _names(prepared):
    return [row[0] for row in prepared.source.rows]


def _lines(prepared):
    return [prepared.source.row_number(i) for i in range(prepared.source.n_rows)]


# ------------------------------------------------------------------ validation
def test_unknown_action_is_rejected():
    prepared = _after([{"action": "reticulate", "at": "Nimbus Forge"}])
    assert prepared.inert() == ["c1"]
    assert "unknown action" in prepared.verdicts["c1"]["why"]
    assert not prepared.changed


@pytest.mark.parametrize("action,extra", [
    ("skip_row", {"at": "Bluedot"}),
    ("dismiss_conflict", {"conflict": "duplicate_row:Customer Name"}),
    ("change_key", {"column": "Phone"}),
])
def test_a_decision_without_a_reason_is_rejected(action, extra):
    prepared = _after([{"action": action, **extra}])
    assert "needs a reason" in prepared.verdicts["c1"]["why"]


def test_a_waiver_must_name_the_conflict():
    prepared = _after([{"action": "dismiss_conflict", "reason": "two entities"}])
    assert "must name the conflict" in prepared.verdicts["c1"]["why"]


def test_set_value_needs_a_column_in_the_sheet():
    prepared = _after([_set("Nimbus Forge", "1", column="Fax")])
    assert "'Fax' is not a column" in prepared.verdicts["c1"]["why"]


def test_set_value_needs_a_value():
    prepared = _after([{"action": "set_value", "at": "Nimbus Forge",
                        "column": "Phone"}])
    assert "needs a value" in prepared.verdicts["c1"]["why"]


def test_an_empty_value_is_a_real_correction():
    """Clearing a cell is not the same as omitting `value`."""
    prepared = _after([_set("Nimbus Forge", "", **{"from": "1111"})])
    assert prepared.applied() == ["c1"]
    assert prepared.source.rows[0][2] == ""


def test_a_bare_number_row_reference_is_rejected():
    """A numeric key value and a row number would otherwise be the same JSON."""
    prepared = _after([_set(5, "4444")])
    assert '{"row": N}' in prepared.verdicts["c1"]["why"]


def test_a_row_reference_of_the_wrong_shape_is_rejected():
    prepared = _after([_set([5], "4444")])
    assert "row reference" in prepared.verdicts["c1"]["why"]


def test_merge_rows_cannot_drop_the_row_it_keeps():
    prepared = _after([{"action": "merge_rows", "keep": "Bluedot",
                        "drop": ["Bluedot"], "note": "oops"}])
    assert "in drop too" in prepared.verdicts["c1"]["why"]


def test_merge_rows_field_overrides_must_be_sheet_columns():
    prepared = _after([{"action": "merge_rows", "keep": "Nimbus Forge",
                        "drop": ["Bluedot"], "field_overrides": {"Fax": "1"}}])
    assert "field_overrides column(s) not in this sheet: Fax" in \
        prepared.verdicts["c1"]["why"]


def test_change_key_needs_a_real_column():
    prepared = _after([{"action": "change_key", "column": "Tax ID",
                        "reason": "names are not unique across branches"}])
    assert "'Tax ID' is not a column" in prepared.verdicts["c1"]["why"]
    ok = _after([{"action": "change_key", "column": "Phone",
                  "reason": "names are not unique across branches"}])
    assert ok.applied() == ["c1"]


def test_a_duplicate_correction_id_is_rejected():
    prepared = _after([_set("Nimbus Forge", "1", id="c1", **{"from": "1111"}),
                       _set("Bluedot", "2", id="c1", **{"from": "5555"})])
    assert "used twice" in prepared.verdicts["c1"]["why"]
    assert len(prepared.corrections) == 2
    assert not prepared.changed


# ------------------------------------------------------------ row verification
def test_a_value_reference_resolves_the_row_it_names():
    prepared = _after([_set("Nimbus Forge", "4444", **{"from": "1111"})])
    assert prepared.applied() == ["c1"]
    assert prepared.source.rows[0][2] == "4444"


def test_an_unknown_key_value_is_inert():
    prepared = _after([_set("Nobody Ltd", "4444")])
    assert prepared.verdicts["c1"]["why"] == "no row has Customer Name = 'Nobody Ltd'"


def test_a_repeated_key_value_is_ambiguous_and_inert():
    rows = [["Same Co", "Company", "1"], ["Same Co", "Company", "2"]]
    prepared = _after([{"action": "skip_row", "at": "Same Co",
                        "reason": "duplicate"}], rows=rows)
    why = prepared.verdicts["c1"]["why"]
    assert "2 rows have Customer Name = 'Same Co'" in why
    assert '{"row": N}' in why                          # points at the way out


def test_blank_key_rows_need_the_position_reference():
    """Identical-looking rows share a key value *by definition*, so a value
    cannot name one of them."""
    rows = [["", "Company", "1"], ["", "Company", "2"]]
    by_value = _after([{"action": "skip_row", "at": "", "reason": "empty"}],
                      rows=rows)
    assert "2 rows have Customer Name = ''" in by_value.verdicts["c1"]["why"]
    by_position = _after([{"action": "skip_row", "at": {"row": 3},
                           "reason": "empty row"}], rows=rows)
    assert by_position.applied() == ["c1"]


def test_a_position_reference_resolves_by_source_line_number():
    prepared = _after([_set({"row": 5}, "3011 0000", column="Customer Name",
                            **{"from": ""})])
    assert prepared.applied() == ["c1"]
    assert _names(prepared)[3] == "3011 0000"


def test_a_position_reference_to_a_missing_line_is_inert():
    prepared = _after([_set({"row": 99}, "4444")])
    assert prepared.verdicts["c1"]["why"] == "the sheet has no row 99"


def test_a_position_reference_goes_inert_once_the_sheet_changed():
    """Inserting a row shifts every number below it, silently, so an edit
    invalidates position references — the hash written with the correction is the
    guard."""
    correction = _set({"row": 5}, "4444", source_sha256="aaa")
    moved = _after([correction], sha256="bbb")
    assert "changed since this correction was written" in moved.verdicts["c1"]["why"]
    same = _after([correction], sha256="aaa")
    assert same.applied() == ["c1"]


def test_a_value_reference_survives_an_edit():
    """It names a row by content, so an unrelated edit cannot move it."""
    prepared = _after([_set("Nimbus Forge", "4444", source_sha256="aaa",
                            **{"from": "1111"})], sha256="bbb")
    assert prepared.applied() == ["c1"]


# ---------------------------------------------------------------- application
def test_set_value_fills_a_blank_cell():
    prepared = _after([_set({"row": 5}, "Kestrel Engineering",
                            column="Customer Name")])
    assert _names(prepared)[3] == "Kestrel Engineering"
    assert prepared.changed


def test_set_value_records_the_replaced_value_at_first_sight():
    prepared = _after([_set({"row": 5}, "Kestrel Engineering",
                            column="Customer Name")])
    assert prepared.corrections[0]["from"] == ""        # the cell was blank


def test_set_value_applies_while_the_cell_still_holds_what_it_replaced():
    prepared = _after([_set("Nimbus Forge", "4444", **{"from": "1111"})])
    assert prepared.applied() == ["c1"]


def test_set_value_is_inert_once_the_cell_holds_something_else():
    prepared = _after([_set("Nimbus Forge", "4444", **{"from": "0000"})])
    assert prepared.inert() == ["c1"]
    assert prepared.verdicts["c1"]["why"] == (
        "the cell now holds '1111', not '0000' as it did when the correction "
        "was written")
    assert prepared.source.rows[0][2] == "1111"          # nothing was applied


def test_two_corrections_on_one_cell_apply_in_file_order():
    """The second one's baseline is already stale, so it cannot double-apply."""
    prepared = _after([_set("Nimbus Forge", "First", **{"from": "1111"}),
                       _set("Nimbus Forge", "Second", **{"from": "1111"})])
    assert prepared.applied() == ["c1"]
    assert prepared.inert() == ["c2"]
    assert prepared.source.rows[0][2] == "First"


def test_skip_row_drops_the_row_and_keeps_source_line_numbers():
    prepared = _after([{"action": "skip_row", "at": "Granite Bay",
                        "reason": "stale row from the 2024 export"}])
    assert _names(prepared) == ["Nimbus Forge", "Harborline", "", "Bluedot"]
    assert _lines(prepared) == [2, 4, 5, 6]              # Bluedot is still line 6


def test_set_value_on_a_skipped_row_is_inert():
    prepared = _after([{"action": "skip_row", "at": "Bluedot", "reason": "junk"},
                       _set("Bluedot", "7777")])
    assert prepared.inert() == ["c2"]
    assert "dropped by an earlier correction" in prepared.verdicts["c2"]["why"]


def test_merge_rows_applies_overrides_and_drops_the_duplicate():
    rows = [row[:] for row in ROWS] + [["Bluedot Logistics", "", "5555"]]
    prepared = _after([{
        "action": "merge_rows", "keep": "Bluedot", "drop": [{"row": 7}],
        "field_overrides": {"Phone": "6666"},
        "note": "same customer with a stale phone",
    }], rows=rows)
    assert prepared.applied() == ["c1"]
    assert _names(prepared) == ["Nimbus Forge", "Granite Bay", "Harborline", "",
                                "Bluedot"]
    assert prepared.source.rows[-1][2] == "6666"        # override on the kept row
    assert _lines(prepared) == [2, 3, 4, 5, 6]


def test_merge_rows_applies_nothing_when_a_dropped_row_is_missing():
    prepared = _after([{"action": "merge_rows", "keep": "Bluedot",
                        "drop": [{"row": 99}],
                        "field_overrides": {"Phone": "6666"}}])
    assert prepared.verdicts["c1"]["why"] == "the sheet has no row 99"
    assert prepared.source.rows == ROWS                  # no partial merge


def test_a_merge_can_name_several_drops():
    prepared = _after([{"action": "merge_rows", "keep": "Nimbus Forge",
                        "drop": ["Granite Bay", "Bluedot"]}])
    assert _names(prepared) == ["Nimbus Forge", "Harborline", ""]


def test_a_revoked_correction_does_not_apply():
    prepared = _after([_set("Nimbus Forge", "4444",
                            revoked_at="2026-09-14T11:02:00+00:00",
                            revoked_reason="wrong row")])
    assert prepared.verdicts == {}
    assert prepared.source.rows == ROWS


# --------------------------------------------------------------- bookkeeping
def test_the_tool_fills_ids_and_provenance():
    prepared = _after([_set("Nimbus Forge", "1"), _set("Bluedot", "2", id="c7")])
    assert [c["id"] for c in prepared.corrections] == ["c1", "c7"]
    assert all(c["created_at"] and c["created_by"] == "human"
               for c in prepared.corrections)
    # a value-keyed correction records the column it was written against
    assert all(c["key_column"] == "Customer Name" for c in prepared.corrections)


def test_a_position_keyed_correction_records_the_hash_it_was_written_under():
    prepared = _after([{"action": "skip_row", "at": {"row": 5},
                        "reason": "empty row"}], sha256="feed")
    assert prepared.corrections[0]["source_sha256"] == "feed"


def test_no_corrections_returns_the_same_table():
    prepared, sheet = _prep([])
    assert prepared.source is sheet
    assert not prepared.changed
    assert prepared.verdicts == {}


def test_inert_corrections_alone_are_not_a_change():
    prepared = _after([_set("Nobody Ltd", "4444")])
    assert not prepared.changed


def test_the_source_table_is_never_mutated():
    sheet = make_sheet(HEADERS, [row[:] for row in ROWS])
    before = [row[:] for row in sheet.rows]
    C.prepare(sheet, _ws([{"action": "skip_row", "at": "Bluedot",
                           "reason": "junk"}, _set("Nimbus Forge", "4444")]),
              sha256="")
    assert sheet.rows == before
    assert sheet.row_numbers is None


def test_the_corrected_copy_shares_the_raw_profiles():
    """Profiles describe the file the operator is being asked to fix."""
    prepared, sheet = _prep([_set("Nimbus Forge", "4444")])
    assert prepared.source.profiles is sheet.profiles


def test_a_rejected_correction_is_flagged_as_invalid():
    """Invalid (a mistake in the worksheet) is distinct from stale (a lifecycle
    state), because only one of them is the operator's typo."""
    prepared = _after([{"action": "reticulate", "at": "Nimbus Forge"}])
    assert prepared.invalid() == ["c1"]
    stale = _after([_set("Nobody Ltd", "4444")])
    assert stale.inert() == ["c1"]
    assert stale.invalid() == []


# -------------------------------------------------------------------- helpers
def test_conflict_key_matches_the_documented_shape():
    assert C.conflict_key({"kind": "duplicate_row", "source": "Name",
                           "target": "customer_name"}) == \
        "duplicate_row:Name:customer_name"
    assert C.conflict_key({"kind": "possible_duplicate_row", "source": "Name"}) == \
        "possible_duplicate_row:Name"
    assert C.conflict_key({"kind": "required_missing", "field": "gender"}) == \
        "required_missing:gender"


def test_worksheet_path_is_doctype_and_source_scoped(tmp_path):
    path = C.worksheet_path("Customer Group", "samples/customers_smb.csv", tmp_path)
    assert path.name == "customer-group-customers-smb.json"
    assert path.parent == tmp_path


def test_load_worksheet_returns_nothing_when_absent(tmp_path):
    assert C.load_worksheet("Customer", "samples/customers.csv", tmp_path) == {}


def test_load_worksheet_reads_corrections(tmp_path):
    path = C.worksheet_path("Customer", "samples/customers.csv", tmp_path)
    path.write_text(json.dumps(_ws([_set("Nimbus Forge", "1")])), encoding="utf-8")
    loaded = C.load_worksheet("Customer", "samples/customers.csv", tmp_path)
    assert loaded["corrections"][0]["action"] == "set_value"


def test_a_corrupt_worksheet_is_an_error_not_a_silent_skip(tmp_path):
    path = C.worksheet_path("Customer", "samples/customers.csv", tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        C.load_worksheet("Customer", "samples/customers.csv", tmp_path)


# ---------------------------------------------------------------- integration
def _customer_engine():
    return MappingEngine(make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True),
        make_field("customer_type", "Customer Type", reqd=True),
        make_field("phone", "Phone"),
    ]))


def test_corrected_rows_reach_the_relational_payloads():
    sheet = make_sheet(HEADERS, [row[:] for row in ROWS])
    engine = _customer_engine()
    plan = engine.suggest(sheet)
    prepared = C.prepare(sheet, _ws([
        _set({"row": 5}, "Kestrel Engineering", column="Customer Name"),
        {"action": "skip_row", "at": "Granite Bay", "reason": "stale"},
    ]), sha256="")

    payloads, errors = engine.build_payloads(prepared.source, plan)

    assert [p["customer_name"] for p in payloads] == \
        ["Nimbus Forge", "Harborline", "Kestrel Engineering", "Bluedot"]
    # the payload keeps its source line number, so a log still points at the
    # line the operator can see
    assert [p["__row"] for p in payloads] == [2, 4, 5, 6]
    assert errors == []


def test_corrected_rows_reach_the_flat_payloads():
    headers = ["Customer Name", "Customer Type", "Customer Group"]
    rows = [["Acme Steel Work", "Company", "Commercial"],
            ["Bluedot", "Company", "Commercial"]]
    sheet = make_sheet(headers, rows)
    prepared = C.prepare(sheet, _ws([{
        "action": "merge_rows", "keep": "Acme Steel Work", "drop": [{"row": 3}],
        "field_overrides": {"Customer Name": "Acme Steel Works"},
    }]), sha256="")

    assert len(flat_payloads(sheet, "Customer")) == 2
    payloads = flat_payloads(prepared.source, "Customer")
    assert len(payloads) == 1
    assert payloads[0]["customer"]["customer_name"] == "Acme Steel Works"


def test_build_plan_builds_payloads_from_the_corrected_rows(cli, monkeypatch):
    """The `erpgen.py` wiring: mapping decisions come from the raw sheet, payloads
    from the corrections — so a corrected cell can never change which column maps
    where."""
    engine = _customer_engine()
    monkeypatch.setattr(cli, "_engine", lambda *a, **kw: (engine, object()))
    monkeypatch.setattr(cli, "_overrides_for", lambda *a, **kw: (None, {}))
    args = cli.build_parser().parse_args(["import", "x.csv", "--doctype", "Customer"])
    sheet = make_sheet(HEADERS, [row[:] for row in ROWS])
    prepared = C.prepare(sheet, _ws([{"action": "skip_row", "at": "Granite Bay",
                                      "reason": "stale"}]), sha256="")

    _, _, _, payloads, _ = cli._build_plan(args, sheet,
                                           payload_source=prepared.source)
    # the blank name row imports no name at all: empty cells are omitted
    assert [p.get("customer_name", "") for p in payloads] == \
        ["Nimbus Forge", "Harborline", "", "Bluedot"]


def test_the_flat_import_imports_the_corrected_rows(monkeypatch):
    """`import_flat_parties` must build from the corrected table while every
    message keeps the source line number."""
    import erpgen.customers_full as cf

    headers = ["Customer Name", "Customer Type", "Email"]
    rows = [["Nimbus Forge", "Company", "a@x.example"],
            ["Bluedot", "Company", "b@y.example"]]
    sheet = make_sheet(headers, rows)
    prepared = C.prepare(sheet, _ws([
        {"action": "merge_rows", "keep": "Nimbus Forge", "drop": [{"row": 3}],
         "field_overrides": {"Email": "ops@nimbus.example"}},
    ]), sha256="")
    captured = {}
    real = cf.build_payloads

    def spy(source, *a, **kw):
        out = real(source, *a, **kw)
        captured["source"] = source
        captured["payloads"] = out
        return out

    monkeypatch.setattr(cf, "build_payloads", spy)
    monkeypatch.setattr(cf, "type_maps", lambda client, party: {})
    import_flat_parties(FakeClient(), sheet, party="Customer", apply=False,
                        prepared=prepared)

    assert [p["customer"]["customer_name"] for p in captured["payloads"]] == \
        ["Nimbus Forge"]
    assert captured["payloads"][0]["contact"]["email_ids"][0]["email_id"] == \
        "ops@nimbus.example"
    assert captured["source"].row_number(0) == 2
