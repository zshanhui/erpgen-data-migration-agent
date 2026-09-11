"""P0: revertible effects — journal parsing, inverse application, LIFO replay,
and the idempotency of reverting twice.

`apply_inverse`/`revert_journal` are exercised against a FakeClient, so these
stay pure unit tests (no live site).
"""
from __future__ import annotations

import json

import pytest

from conftest import FakeClient, delete_record_effect, write_journal
from erpgen.journal import (MigrationJournal, apply_inverse, describe_inverse,
                            mark_reverted, parse_journal, revert_journal)


# ------------------------------------------------------------------ parsing
def test_parse_journal_splits_run_start_effects_and_extras(tmp_path):
    p = write_journal(
        tmp_path / "j.jsonl",
        [delete_record_effect("Item", "A"), delete_record_effect("Item", "B")],
        run_id="j1", doctype="Item",
        extra_lines=[json.dumps({"event": "run_end", "effects": 2}),
                     json.dumps({"event": "revert", "reverted": 2, "status": "ok"})],
    )
    data = parse_journal(p)
    assert data["run_start"]["run_id"] == "j1"
    assert [e["inverse"]["name"] for e in data["effects"]] == ["A", "B"]
    assert [e["event"] for e in data["extra"]] == ["run_end", "revert"]


def test_parse_journal_tolerates_corrupt_and_blank_lines(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("Item", "A")],
                      raw_prefix="garbage line\n\n{not json\n")
    data = parse_journal(p)
    assert len(data["effects"]) == 1
    assert data["extra"] == []


def test_parse_journal_on_empty_file(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    data = parse_journal(p)
    assert data == {"run_start": None, "effects": [], "extra": []}


# ------------------------------------------------------------ descriptions
@pytest.mark.parametrize("inv,expected", [
    ({"op": "delete_record", "doctype": "UOM", "name": "Dozen"}, "delete record UOM/Dozen"),
    ({"op": "delete_custom_field", "name": "Item-notes"},
     "drop custom field Item-notes  (DROPS COLUMN + DATA)"),
    ({"op": "restore_override", "doctype": "Item", "column": "Group", "previous": "x"},
     "restore override Item.Group -> 'x'"),
    ({"op": "restore_override", "doctype": "Item", "column": "Group", "previous": None},
     "restore override Item.Group (remove override)"),
])
def test_describe_inverse(inv, expected):
    assert describe_inverse(inv) == expected


def test_describe_inverse_unknown_op():
    assert describe_inverse({"op": "explode"}).startswith("unknown inverse op")


# --------------------------------------------------------- inverse applying
def test_apply_inverse_dry_run_does_not_touch_the_client(fake_client):
    ok, err = apply_inverse(fake_client, {"op": "delete_record", "doctype": "Item",
                                          "name": "A"}, apply=False)
    assert (ok, err) == (True, "")
    assert fake_client.calls == []


def test_apply_inverse_deletes_and_removes_the_record(fake_client):
    client = FakeClient(existing={("Item", "A")})
    ok, err = apply_inverse(client, {"op": "delete_record", "doctype": "Item", "name": "A"})
    assert (ok, err) == (True, "")
    assert client.existing == set()


def test_apply_inverse_treats_missing_record_as_success(fake_client):
    """Reverting twice must not report a spurious failure."""
    ok, err = apply_inverse(fake_client, {"op": "delete_record", "doctype": "Item",
                                          "name": "already-gone"})
    assert (ok, err) == (True, "")


def test_apply_inverse_treats_missing_custom_field_as_success(fake_client):
    ok, err = apply_inverse(fake_client, {"op": "delete_custom_field", "name": "Item-x"})
    assert (ok, err) == (True, "")


def test_apply_inverse_reports_a_real_error():
    client = FakeClient(fail_with={"UOM": "LinkExistsError: You can disable this UOM"})
    ok, err = apply_inverse(client, {"op": "delete_record", "doctype": "UOM", "name": "Dozen"})
    assert ok is False
    assert "LinkExistsError" in err


def test_apply_inverse_unknown_op_is_a_failure(fake_client):
    ok, err = apply_inverse(fake_client, {"op": "nope"})
    assert ok is False and "unknown inverse op" in err


def test_apply_inverse_restore_override_removes_or_restores(tmp_path):
    from erpgen.overrides import load_overrides
    ov = tmp_path / "mapping-overrides.json"
    ov.write_text(json.dumps({"Item": {"mappings": {"Group": "item_group",
                                                    "UoM": "stock_uom"}},
                              "defaults": {}, "value_maps": {}}), encoding="utf-8")
    ok, _ = apply_inverse(None, {"op": "restore_override", "doctype": "Item",
                                 "column": "Group", "previous": None, "path": str(ov)})
    assert ok and load_overrides(ov)["Item"]["mappings"] == {"UoM": "stock_uom"}

    ok, _ = apply_inverse(None, {"op": "restore_override", "doctype": "Item",
                                 "column": "Group", "previous": "customer_group",
                                 "path": str(ov)})
    assert ok and load_overrides(ov)["Item"]["mappings"]["Group"] == "customer_group"


# ------------------------------------------------------------------- replay
def test_revert_journal_replays_inverses_newest_first(tmp_path):
    p = write_journal(tmp_path / "j.jsonl",
                      [delete_record_effect("Item", "A"),
                       delete_record_effect("Item", "B"),
                       delete_record_effect("Item", "C")])
    client = FakeClient(existing={("Item", "A"), ("Item", "B"), ("Item", "C")})
    res = revert_journal(client, p, apply=True)
    assert [c[2] for c in client.calls] == ["C", "B", "A"]
    assert res["applied"] == res["total"] == 3
    assert res["failed"] == []


def test_revert_journal_dry_run_lists_without_acting(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("Item", "A")])
    client = FakeClient(existing={("Item", "A")})
    res = revert_journal(client, p, apply=False)
    assert client.calls == []
    assert res["results"][0]["description"] == "delete record Item/A"
    assert "revert" not in [e["event"] for e in parse_journal(p)["extra"]]
    assert client.existing == {("Item", "A")}


def test_revert_journal_marks_a_successful_revert(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("Item", "A")], run_id="j1")
    res = revert_journal(FakeClient(existing={("Item", "A")}), p, apply=True)
    assert res["applied"] == 1
    marker = [e for e in parse_journal(p)["extra"] if e["event"] == "revert"][-1]
    assert marker["reverted"] == 1 and marker["status"] == "ok"
    assert marker["run_id"] == "j1"


def test_second_revert_is_a_no_op(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("Item", "A")])
    revert_journal(FakeClient(existing={("Item", "A")}), p, apply=True)

    client = FakeClient()
    res = revert_journal(client, p, apply=True)
    assert res["applied"] == 0
    assert res["already_reverted"] is not None
    assert client.calls == [], "already-reverted journal must not be replayed"


def test_force_replays_an_already_reverted_journal(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("Item", "A")])
    revert_journal(FakeClient(existing={("Item", "A")}), p, apply=True)
    client = FakeClient()  # record already gone -> 404s are tolerated
    res = revert_journal(client, p, apply=True, force=True)
    assert res["applied"] == 1 and res["failed"] == []
    assert len(client.calls) == 1


def test_failed_revert_is_not_marked_so_it_can_be_retried(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("UOM", "Dozen")])
    client = FakeClient(fail_with={"UOM": "LinkExistsError: in use"})
    res = revert_journal(client, p, apply=True)
    assert res["applied"] == 0 and len(res["failed"]) == 1
    assert [e for e in parse_journal(p)["extra"] if e["event"] == "revert"] == []


def test_partial_marker_does_not_count_as_reverted(tmp_path):
    p = write_journal(tmp_path / "j.jsonl",
                      [delete_record_effect("Item", "A"), delete_record_effect("Item", "B")])
    mark_reverted(p, "j1", 1, "ok")  # only one of two effects
    client = FakeClient(existing={("Item", "A"), ("Item", "B")})
    res = revert_journal(client, p, apply=True)
    assert "already_reverted" not in res
    assert len(client.calls) == 2


def test_marker_with_a_non_ok_status_is_not_reverted(tmp_path):
    """A marker is only authoritative when the revert actually succeeded."""
    p = write_journal(tmp_path / "j.jsonl", [delete_record_effect("Item", "A")])
    mark_reverted(p, "j1", 1, "failed")
    client = FakeClient(existing={("Item", "A")})
    res = revert_journal(client, p, apply=True)
    assert "already_reverted" not in res
    assert len(client.calls) == 1


def test_revert_of_empty_journal(tmp_path):
    p = write_journal(tmp_path / "j.jsonl", [])
    res = revert_journal(FakeClient(), p, apply=True)
    assert res["total"] == 0 and res["applied"] == 0


# ------------------------------------------------------- MigrationJournal
def test_migration_journal_counts_and_closes(tmp_path):
    j = MigrationJournal(tmp_path, doctype="Item Group", source="s.csv")
    j.record_created("Item Group", "Tooling")
    j.custom_field_created("Item", "notes", "Item-notes", label="Notes")
    assert j.count == 2
    j.close()
    data = parse_journal(j.path)
    assert data["run_start"]["doctype"] == "Item Group"
    assert len(data["effects"]) == 2
    assert [e["event"] for e in data["extra"]] == ["run_end"]
    assert j.path.name.startswith("journal-item-group-")


def test_migration_journal_accepts_an_explicit_run_id(tmp_path):
    j = MigrationJournal(tmp_path, doctype="Item", run_id="explicit")
    assert j.run_id == "explicit"
    j.close()
    assert parse_journal(j.path)["run_start"]["run_id"] == "explicit"
