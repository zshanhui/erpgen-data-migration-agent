"""P0: the revertible-effect side of MigrationContext (journal-compatible run
files): effect sequencing across processes, replay on reopen, close semantics,
tolerance of a corrupted log, and the read views.
"""
from __future__ import annotations

import json

import pytest

from erpgen.context import load_run, resolve_run


def _events(path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------- sequencing
def test_effect_sequence_continues_across_commands(mkctx):
    """Each command used to restart at #1; the run must number effects as one
    migration."""
    c1 = mkctx("run1")
    assert c1.effect("record_create", {"op": "delete_record"}, doctype="Item", name="A") == 1
    c1.close()

    c2 = mkctx("run1")
    assert c2.effects == 1, "effect count not restored"
    assert c2.effect("record_create", {"op": "delete_record"}, doctype="Item", name="B") == 2
    c2.close()

    seqs = [e["seq"] for e in load_run("run1", c2.path.parent)["effects"]]
    assert seqs == [1, 2]


def test_effect_seq_is_monotonic_over_many_commands(mkctx, tmp_path):
    for i in range(4):
        c = mkctx("run1")
        c.record_created("Item", f"I-{i}")
        c.close()
    assert [e["seq"] for e in load_run("run1", tmp_path)["effects"]] == [1, 2, 3, 4]


def test_typed_effect_helpers_record_their_inverse(mkctx):
    c = mkctx("run1")
    c.record_created("UOM", "Dozen")
    c.custom_field_created("Item", "notes", "Item-notes", label="Notes")
    c.override_set("Item", "Group", "item_group", "customer_group", "ov.json")
    c.close()
    invs = [e["inverse"] for e in load_run("run1", c.path.parent)["effects"]]
    assert invs[0] == {"op": "delete_record", "doctype": "UOM", "name": "Dozen"}
    assert invs[1] == {"op": "delete_custom_field", "name": "Item-notes"}
    assert invs[2] == {"op": "restore_override", "doctype": "Item", "column": "Group",
                       "previous": "customer_group", "path": "ov.json"}


def test_custom_field_label_defaults_to_fieldname(mkctx):
    c = mkctx("run1")
    c.custom_field_created("Item", "notes", "Item-notes")
    c.close()
    eff = load_run("run1", c.path.parent)["effects"][0]
    assert eff["label"] == "notes"
    assert eff["fieldname"] == "notes"


# ------------------------------------------------------------- read views
def test_load_run_accepts_run_id_or_path(mkctx):
    c = mkctx("run1")
    c.record_created("Item", "A")
    c.close()
    by_id = load_run("run1", c.path.parent)
    by_path = load_run(str(c.path))
    assert by_id["run_id"] == by_path["run_id"] == "run1"
    assert by_id["path"] == str(c.path)


def test_run_start_stamps_doctype_only_when_unambiguous(mkctx):
    c = mkctx("run1", doctypes=["Item"])
    c.close()
    c2 = mkctx("run2", doctypes=["Item", "Customer"])
    c2.close()
    starts = {e["run_id"]: e for e in _events(c.path) if e["event"] == "run_start"}
    assert starts["run1"]["doctype"] == "Item"
    assert starts["run1"]["doctypes"] == ["Item"]
    starts2 = {e["run_id"]: e for e in _events(c2.path) if e["event"] == "run_start"}
    assert starts2["run2"]["doctype"] is None
    assert starts2["run2"]["doctypes"] == ["Customer", "Item"]


def test_run_start_records_source_and_command(mkctx):
    c = mkctx("run1", source="samples/items.csv", command="map",
              base_url="http://localhost:8082")
    c.close()
    start = [e for e in _events(c.path) if e["event"] == "run_start"][-1]
    assert start["source"] == "samples/items.csv"
    assert start["command"] == "map"
    assert start["base_url"] == "http://localhost:8082"


def test_config_delta_is_recorded_and_exposed(mkctx):
    c = mkctx("run1")
    c.config_delta("mapping-overrides.json", "Item", {"mappings": {"Group": "item_group"}})
    c.close()
    data = load_run("run1", c.path.parent)
    assert data["effects"] == []
    assert len(data["config_delta"]) == 1
    delta = data["config_delta"][0]
    assert delta["path"] == "mapping-overrides.json"
    assert delta["doctype"] == "Item"
    assert delta["changes"] == {"mappings": {"Group": "item_group"}}


# ------------------------------------------------------------------ closing
def test_close_is_idempotent(mkctx):
    """The agent tail closed a context twice and crashed with
    'I/O operation on closed file'."""
    c = mkctx("run1")
    c.record_created("Item", "A")
    c.close()
    c.close()  # must not raise
    ends = [e for e in _events(c.path) if e["event"] == "run_end"]
    assert len(ends) == 1
    assert ends[0]["effects"] == 1
    assert ends[0]["pending"] == 0


def test_close_reports_pending_count(mkctx):
    c = mkctx("run1")
    c.add_requirements([{"kind": "unmapped_column", "severity": "info", "source": "Notes"}])
    c.close()
    assert [e for e in _events(c.path) if e["event"] == "run_end"][0]["pending"] == 1


# ------------------------------------------------------------- robustness
def test_corrupt_lines_do_not_break_reopen(mkctx):
    c = mkctx("run1")
    c.add_requirements([{"kind": "unmapped_column", "severity": "info", "source": "Notes"}])
    c.record_created("Item", "A")
    c.close()
    with c.path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write('{"event": "effect", "truncated"\n')
        fh.write("\n")

    c2 = mkctx("run1")
    assert c2.effects == 1
    assert len(c2.pending_requirements()) == 1
    assert c2.effect("record_create", {"op": "delete_record"}, doctype="Item", name="B") == 2


def test_missing_run_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_run("nope", tmp_path)


def test_slug_keeps_run_ids_inside_the_log_dir(tmp_path):
    """A run id is used to build a filename: it must not escape log_dir."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "secret.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        resolve_run("../secret", logs)
