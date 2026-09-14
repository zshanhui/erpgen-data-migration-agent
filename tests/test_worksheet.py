"""Phase 5b: the worksheet — statuses, the gate, and merge-forward.

The property under test: the detectors read the raw source, so a corrected
conflict is re-detected every run; status is derived from re-detection plus
whether each correction could be applied. Conflicts the detectors no longer
report persist as `resolved` — except when the sheet's hash changed under a
correction, which is `stale` and blocks.

Pure where possible; the file-writing tests use tmp_path so nothing touches the
repo's `worksheets/` or `analysis/`.
"""
from __future__ import annotations

import json

import pytest
from conftest import FakeClient, make_field, make_meta, make_sheet

from erpgen import corrections as C
from erpgen.analysis import build_analysis, save_analysis
from erpgen.mapper import MappingEngine

HEADERS = ["Customer Name", "Customer Type", "Phone"]
ROWS = [["Acme", "Company", "1111"], ["", "Company", ""]]  # lines 2, 3


def _conflict(kind, source, target="", **extra):
    c = {"kind": kind, "severity": "error", "source": source}
    if target:
        c["target"] = target
    c.update(extra)
    return c


def _set(row=3, value="Bee", conflict="missing_value:Customer Name:customer_name",
         **extra):
    return {"action": "set_value", "at": {"row": row}, "column": "Customer Name",
            "value": value, "conflict": conflict, **extra}


def _statuses(conflicts, corrections=None, *, previous=None, previous_hash="",
              sha256=""):
    sheet = make_sheet(HEADERS, [r[:] for r in ROWS])
    prepared = C.prepare(sheet, {"source": {"key_column": "Customer Name"},
                                 "corrections": corrections or []},
                         sha256=sha256)
    return C.statuses(conflicts, prepared, sha256=sha256, previous=previous,
                      previous_hash=previous_hash)


MISSING = lambda: _conflict("missing_value", "Customer Name", "customer_name")


# -------------------------------------------------------------------- statuses
def test_open_without_a_correction():
    [c] = _statuses([MISSING()])
    assert c["status"] == "open"
    assert c["resolution"] is None


def test_corrected_when_the_answering_correction_applies():
    [c] = _statuses([MISSING()], [_set()])
    assert c["status"] == "corrected"
    assert c["resolution"] == {"by": "correction", "ids": ["c1"]}


def test_stale_when_the_answering_correction_is_inert():
    [c] = _statuses([MISSING()], [_set(row=99)])
    assert c["status"] == "stale"


def test_waived_by_a_dismiss_conflict():
    [c] = _statuses([MISSING()], [{"action": "dismiss_conflict",
                                   "conflict": "missing_value:Customer Name:customer_name",
                                   "reason": "taken from the PO"}])
    assert c["status"] == "waived"
    assert c["resolution"] == {"by": "dismiss_conflict",
                               "reason": "taken from the PO", "id": "c1"}


def test_a_waiver_wins_over_an_inert_correction():
    """A decision to accept the conflict beats a broken fix for it."""
    [c] = _statuses([MISSING()], [
        _set(row=99),
        {"action": "dismiss_conflict",
         "conflict": "missing_value:Customer Name:customer_name",
         "reason": "not needed"},
    ])
    assert c["status"] == "waived"


def test_resolved_when_no_longer_detected():
    [c] = _statuses([], previous=[MISSING()])
    assert c["status"] == "resolved"


def test_resolved_turns_stale_when_the_sheet_changed_under_a_correction():
    """The operator fixed the sheet, but the correction's bookkeeping is now
    suspect (a position may point elsewhere) — surface it rather than pass."""
    [c] = _statuses([], corrections=[_set()], previous=[MISSING()],
                    previous_hash="old", sha256="new")
    assert c["status"] == "stale"


def test_resolved_without_a_correction_stays_resolved_across_an_edit():
    """A vanished conflict with nothing to be inert is just... fixed."""
    [c] = _statuses([], previous=[MISSING()], previous_hash="old", sha256="new")
    assert c["status"] == "resolved"


def test_history_is_recorded_then_carried_forward():
    first = _statuses([MISSING()])
    assert [e["event"] for e in first[0]["history"]] == ["detected"]

    again = _statuses([MISSING()], previous=first)
    assert [e["event"] for e in again[0]["history"]] == ["detected"]

    fixed = _statuses([MISSING()], [_set()], previous=again)
    assert [e["event"] for e in fixed[0]["history"]] == ["detected", "corrected"]

    resolved = _statuses([], previous=fixed)
    assert [e["event"] for e in resolved[0]["history"]] == \
        ["detected", "corrected", "resolved"]


def test_history_records_a_resolution_then_a_reopen():
    fixed = _statuses([MISSING()], [_set()])
    back = _statuses([MISSING()], previous=fixed)
    assert [e["event"] for e in back[0]["history"]] == \
        ["detected", "corrected", "open"]


# ----------------------------------------------------- change_key and detection
def test_change_key_retargets_the_cleaning_key_without_an_id_column():
    """`change_key` only moves the review key: the import's id column is the
    caller's business, not the correction's."""
    sheet = make_sheet(["Customer Name", "Tax ID"],
                       [["Acme", "SG-1"], ["Acme", "SG-2"]])
    prepared = C.prepare(sheet, {"source": {"key_column": "Customer Name"},
                                 "corrections": [
                                     {"action": "change_key", "column": "Tax ID",
                                      "reason": "names repeat across branches"}]})
    assert prepared.key_column == "Tax ID"
    assert prepared.key_changed


def test_change_key_does_not_stale_a_correction_written_before_it():
    """A value-keyed correction records the column it was written against, so a
    later `change_key` retargets what follows it, not what precedes it."""
    sheet = make_sheet(["Customer Name", "Tax ID", "Phone"],
                       [["Acme", "SG-1", ""], ["Bee", "SG-2", ""]])
    prepared = C.prepare(sheet, {
        "source": {"key_column": "Customer Name"},
        "corrections": [
            {"action": "set_value", "at": "Acme", "column": "Phone", "value": "1111"},
            {"action": "change_key", "column": "Tax ID",
             "reason": "names repeat across branches"},
        ]}, sha256="")
    assert prepared.key_column == "Tax ID"               # retargeted
    assert prepared.corrections[0]["key_column"] == "Customer Name"  # recorded
    assert prepared.verdicts["c1"]["applicable"] is True  # still applies
    assert prepared.source.rows[0][2] == "1111"


# ---------------------------------------------------------------- the document
def _customer_engine():
    return MappingEngine(make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True),
        make_field("customer_type", "Customer Type", reqd=True),
        make_field("phone", "Phone"),
    ]))


def test_build_analysis_returns_the_worksheet_document(tmp_path):
    engine = _customer_engine()
    sheet = make_sheet(HEADERS, [r[:] for r in ROWS])
    plan = engine.suggest(sheet)
    analysis = build_analysis(FakeClient(), sheet, plan, engine,
                              id_column="Customer Name",
                              source_path="samples/none.csv",
                              key_source="mapped",
                              worksheet_dir=tmp_path / "worksheets")

    assert analysis["source"]["path"] == "samples/none.csv"
    assert analysis["source"]["key_column"] == "Customer Name"
    assert analysis["source"]["key_source"] == "mapped"
    assert analysis["source"]["sha256"] == ""           # file does not exist
    assert analysis["generator"]["k"] == 2
    by_kind = {c["kind"]: c for c in analysis["conflicts"]}
    assert by_kind["missing_value"]["status"] == "open"
    assert by_kind["missing_value"]["history"][0]["event"] == "detected"
    assert analysis["corrections"] == []


def test_build_analysis_merges_corrections_forward(tmp_path):
    wdir = tmp_path / "worksheets"
    wdir.mkdir()
    C.worksheet_path("Customer", "samples/none.csv", wdir).write_text(
        json.dumps({
            "doctype": "Customer",
            "source": {"key_column": "Customer Name"},
            "corrections": [
                {"action": "skip_row", "at": "Acme", "reason": "dup",
                 "revoked_at": "2026-09-15T00:00:00+00:00",
                 "revoked_reason": "wrong row"},
                _set(),
            ],
        }), encoding="utf-8")

    engine = _customer_engine()
    sheet = make_sheet(HEADERS, [r[:] for r in ROWS])
    plan = engine.suggest(sheet)
    analysis = build_analysis(FakeClient(), sheet, plan, engine,
                              id_column="Customer Name",
                              source_path="samples/none.csv",
                              worksheet_dir=wdir)

    # revoked correction survives; the active one is refreshed with tool fields
    revoked = [c for c in analysis["corrections"] if c.get("revoked_at")]
    active = [c for c in analysis["corrections"] if not c.get("revoked_at")]
    assert len(revoked) == 1 and len(active) == 1
    assert active[0]["id"] == "c1"
    assert active[0]["from"] == ""                       # the blank it replaced

    miss = [c for c in analysis["conflicts"] if c["kind"] == "missing_value"][0]
    assert miss["status"] == "corrected"


# ------------------------------------------------------------------ retention
def test_save_analysis_writes_the_stable_worksheet_and_never_prunes_it(tmp_path):
    analysis = {"doctype": "Customer",
                "source": {"path": "samples/customers.csv"}}
    adir = tmp_path / "analysis"
    wdir = tmp_path / "worksheets"
    save_analysis(analysis, adir, wdir)
    ws = wdir / "customer-customers.json"
    assert ws.exists()

    for _ in range(11):
        save_analysis(analysis, adir, wdir)
    assert ws.exists()                                   # worksheet survives
    assert len(list(adir.glob("analysis-*.json"))) == 10  # snapshots pruned


def test_save_analysis_skips_a_worksheet_without_a_source(tmp_path):
    save_analysis({"doctype": "Item"}, tmp_path, tmp_path / "worksheets")
    assert list((tmp_path / "worksheets").glob("*.json")) == []


# ------------------------------------------------------------ the import gate
def test_the_gate_blocks_only_open_or_stale_errors(cli):
    analysis = {"conflicts": [
        _conflict("duplicate_row", "Customer Name", "customer_name",
                  status="open"),
        _conflict("missing_value", "Customer Type", "customer_type",
                  status="stale"),
        _conflict("required_missing", "gender", status="corrected"),
        _conflict("link_value_conflict", "Group", "customer_group",
                  status="waived"),
        _conflict("possible_duplicate_row", "Customer Name", status="open",
                  severity="warning"),
    ]}
    blocked = cli._blocking_conflicts(analysis)
    assert [(c["kind"], c["status"]) for c in blocked] == \
        [("duplicate_row", "open"), ("missing_value", "stale")]


def test_a_missing_status_reads_as_open(cli):
    """Old snapshots predate statuses; a conflict without one was always open."""
    analysis = {"conflicts": [_conflict("duplicate_row", "Customer Name",
                                        "customer_name")]}
    assert len(cli._blocking_conflicts(analysis)) == 1


# ---------------------------------------------------------- requirement closure
def test_a_correction_closes_a_requirement_by_identity(mkctx):
    ctx = mkctx()
    ctx.add_requirements([MISSING()])
    assert ctx.pending_requirements()

    assert ctx.satisfy_conflict(MISSING(), via="correction") is True
    assert ctx.pending_requirements() == []


def test_satisfy_conflict_ignores_an_unrelated_conflict(mkctx):
    ctx = mkctx()
    ctx.add_requirements([_conflict("duplicate_row", "Customer Name",
                                    "customer_name")])
    assert ctx.satisfy_conflict(MISSING()) is False
    assert ctx.pending_requirements()


# --------------------------------------------------------------- latest_analysis
def test_latest_analysis_resolves_the_worksheet_first(agent_mod, tmp_path,
                                                      monkeypatch):
    monkeypatch.setattr(agent_mod, "ROOT", tmp_path)
    wdir = tmp_path / "worksheets"
    wdir.mkdir()
    ws = wdir / "customer-customers.json"
    ws.write_text(json.dumps({"doctype": "Customer",
                              "source": {"path": "samples/customers.csv"},
                              "n": 1}), encoding="utf-8")

    got = agent_mod.latest_analysis("Customer", "samples/customers.csv")
    assert got["n"] == 1


def test_latest_analysis_refuses_to_pick_between_two_sources(agent_mod, tmp_path,
                                                             monkeypatch, capsys):
    monkeypatch.setattr(agent_mod, "ROOT", tmp_path)
    wdir = tmp_path / "worksheets"
    wdir.mkdir()
    for src in ("customers.csv", "customers_e2e.csv"):
        (wdir / f"customer-{src.split('.')[0]}.json").write_text(
            json.dumps({"doctype": "Customer", "source": {"path": f"samples/{src}"}}),
            encoding="utf-8")

    assert agent_mod.latest_analysis("Customer") is None
    assert "2 worksheets for 'Customer'" in capsys.readouterr().err


def test_latest_analysis_falls_back_to_a_snapshot(agent_mod, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr(agent_mod, "ROOT", tmp_path)
    adir = tmp_path / "analysis"
    adir.mkdir()
    (adir / "analysis-customer-20260915-000000000000.json").write_text(
        json.dumps({"doctype": "Customer", "n": 2}), encoding="utf-8")

    assert agent_mod.latest_analysis("Customer")["n"] == 2
