"""Phase 5c: authoring a correction — and reverting it.

The `correct` command is how a correction gets written (by a human or the agent),
so it is journaled like any other effect: its inverse revokes the correction in
the worksheet. That is what makes `revert` undo a correction rather than only the
records it caused to be imported.
"""
from __future__ import annotations

import json

import pytest
from conftest import FakeClient

from erpgen import corrections as C
from erpgen.journal import apply_inverse, describe_inverse


def _worksheet(path, headers=("Customer Name", "Customer Type"),
               corrections=None):
    path.write_text(json.dumps({
        "doctype": "Customer",
        "source": {"path": "samples/customers.csv", "key_column": "Customer Name"},
        "column_profiles": [{"header": h} for h in headers],
        "corrections": corrections or [],
    }), encoding="utf-8")


def _skip(row=5, **extra):
    return {"action": "skip_row", "at": {"row": row}, "reason": "duplicate row",
            "conflict": "duplicate_row:Customer Name:customer_name", **extra}


# ------------------------------------------------------------ add_correction
def test_add_correction_appends_with_an_id_and_provenance(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp)
    saved, created = C.add_correction(wp, _skip())
    assert created is True
    assert saved["id"] == "c1"
    assert saved["created_by"] == "human"
    assert saved["created_at"]

    data = json.loads(wp.read_text())
    assert [c["id"] for c in data["corrections"]] == ["c1"]


def test_add_correction_is_idempotent(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp)
    first, created1 = C.add_correction(wp, _skip())
    again, created2 = C.add_correction(wp, _skip())
    assert created1 is True and created2 is False
    assert again["id"] == first["id"]                      # the same entry, not a twin
    assert len(json.loads(wp.read_text())["corrections"]) == 1


def test_a_revoked_correction_can_be_re_authored(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp, corrections=[_skip(id="c1", revoked_at="2026-09-15T00:00:00+00:00",
                                       revoked_reason="wrong row")])
    _, created = C.add_correction(wp, _skip())
    assert created is True                                 # fresh decision, new entry
    assert [c["id"] for c in json.loads(wp.read_text())["corrections"]] == ["c1", "c2"]


def test_identical_meaning_with_different_prose_still_dedupes(tmp_path):
    """`note`/`reason` are prose, not meaning: the same edit is the same edit."""
    wp = tmp_path / "ws.json"
    _worksheet(wp)
    C.add_correction(wp, _skip(note="first wording"))
    _, created = C.add_correction(wp, _skip(note="second wording"))
    assert created is False


def test_add_correction_numbers_ids_after_existing_ones(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp, corrections=[{"id": "c7", "action": "set_value", "at": {"row": 1},
                                 "column": "Customer Name", "value": "x"}])
    saved, _ = C.add_correction(wp, _skip())
    assert saved["id"] == "c1"


def test_add_correction_rejects_a_bad_correction(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp)
    with pytest.raises(ValueError, match="needs a reason"):
        C.add_correction(wp, {"action": "skip_row", "at": {"row": 5}})
    with pytest.raises(ValueError, match="not a column"):
        C.add_correction(wp, {"action": "set_value", "at": {"row": 5},
                              "column": "Fax", "value": "x"})
    # nothing was written by a rejected correction
    assert json.loads(wp.read_text())["corrections"] == []


def test_add_correction_needs_a_worksheet(tmp_path):
    with pytest.raises(ValueError, match="run `map` first"):
        C.add_correction(tmp_path / "missing.json", _skip())


# --------------------------------------------------------------------- revoke
def test_revoke_marks_a_correction_revoked(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp, corrections=[_skip(id="c1")])
    assert C.revoke(wp, "c1") is True
    data = json.loads(wp.read_text())
    assert data["corrections"][0]["revoked_at"]
    assert data["corrections"][0]["revoked_reason"] == "reverted"


def test_revoke_is_a_no_op_for_a_missing_or_already_revoked_correction(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp, corrections=[_skip(id="c1", revoked_at="2026-09-15T00:00:00+00:00",
                                       revoked_reason="wrong")])
    assert C.revoke(wp, "c1") is False        # already revoked
    assert C.revoke(wp, "ghost") is False     # unknown


# ------------------------------------------------------------- revert wiring
def test_revert_replays_a_correction_revoke(tmp_path):
    wp = tmp_path / "ws.json"
    _worksheet(wp, corrections=[_skip(id="c1")])
    inv = {"op": "correction_revoke", "path": str(wp), "correction_id": "c1"}
    ok, err = apply_inverse(FakeClient(), inv, apply=True)
    assert ok and err == ""
    assert json.loads(wp.read_text())["corrections"][0]["revoked_reason"] == "reverted"


def test_describe_inverse_names_a_correction():
    inv = {"op": "correction_revoke", "path": "worksheets/customer-customers.json",
           "correction_id": "c3"}
    assert describe_inverse(inv) == \
        "revoke correction c3 in worksheets/customer-customers.json"


# ------------------------------------------------------------------ CLI verb
def test_correct_cli_records_and_journals(cli, tmp_path):
    wdir = tmp_path / "worksheets"
    wdir.mkdir()
    _worksheet(C.worksheet_path("Customer", "samples/customers.csv", wdir))

    args = cli.build_parser().parse_args(
        ["--log-dir", str(tmp_path / "logs"),
         "correct", "samples/customers.csv", "--doctype", "Customer",
         "--json", json.dumps(_skip())])
    args.worksheet_dir = str(wdir)

    assert cli.cmd_correct(args) == 0

    data = json.loads((wdir / "customer-customers.json").read_text())
    assert data["corrections"][0]["id"] == "c1"

    journal = sorted((tmp_path / "logs").glob("journal-customer-*.jsonl"))[-1]
    effects = [json.loads(line) for line in journal.read_text().splitlines()
               if line.strip() and json.loads(line).get("event") == "effect"]
    assert effects[0]["kind"] == "correction_add"
    assert effects[0]["inverse"] == {"op": "correction_revoke",
                                     "path": str(wdir / "customer-customers.json"),
                                     "correction_id": "c1"}


def test_correct_cli_rejects_a_correction_without_a_worksheet(cli, tmp_path):
    args = cli.build_parser().parse_args(
        ["--log-dir", str(tmp_path / "logs"),
         "correct", "samples/customers.csv", "--doctype", "Customer",
         "--json", json.dumps(_skip())])
    args.worksheet_dir = str(tmp_path / "worksheets")
    assert cli.cmd_correct(args) == 2


# ------------------------------------------------- parallel authoring (race)
def _parallel_add(path: str, idx: int) -> None:
    """One distinct correction from a child process; id assignment must not race."""
    from erpgen import corrections as C

    corr = {"action": "set_value", "at": {"row": idx + 2},
            "column": "Customer Name", "value": f"v{idx}",
            "conflict": "missing_value:Customer Name:customer_name"}
    C.add_correction(path, corr)


def test_parallel_adds_do_not_lose_writes(tmp_path):
    """The agent fires `correct` as parallel subprocesses; each must survive.

    `add_correction` is a load -> pick id -> save; un-locked, two processes read
    the same file, pick the same `c{n}`, and the later write drops the earlier.
    """
    import multiprocessing as mp

    wp = tmp_path / "ws.json"
    _worksheet(wp)
    n = 8
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_parallel_add, args=(str(wp), i)) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0

    ids = [c["id"] for c in json.loads(wp.read_text())["corrections"]]
    assert len(ids) == n, f"lost writes: {len(ids)} of {n}"
    assert len(set(ids)) == n, f"duplicate ids: {ids}"
