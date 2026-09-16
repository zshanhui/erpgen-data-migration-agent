"""P0: revertible effects — journal parsing, inverse application, LIFO replay,
and the idempotency of reverting twice.

`apply_inverse`/`revert_journal` are exercised against a FakeClient, so these
stay pure unit tests (no live site).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

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
    j.record_created("Item", "A")           # an effect is what creates the file
    j.close()
    assert parse_journal(j.path)["run_start"]["run_id"] == "explicit"


def test_an_override_effect_records_the_mapping_it_replaced(tmp_path):
    """`set-mapping` is a decision, and its inverse is the decision it overrode —
    `previous=None` meaning "this column had no override before"."""
    j = MigrationJournal(tmp_path, doctype="Customer", source="s.csv")

    j.override_set("Customer", "Group", "customer_group", None, "ov.json")
    j.override_set("Customer", "Tier", "customer_group", "tier", "ov.json")
    j.close()

    invs = [e["inverse"] for e in parse_journal(j.path)["effects"]]
    assert invs[0] == {"op": "restore_override", "doctype": "Customer",
                       "column": "Group", "previous": None, "path": "ov.json"}
    assert invs[1]["previous"] == "tier", "restoring puts the old target back"
    assert "restore override Customer.Tier" in describe_inverse(invs[1])


def test_a_journal_with_no_effects_leaves_no_file(tmp_path):
    """A run that changed nothing has nothing to undo, and an empty journal only
    makes `--latest` point at a file with nothing in it."""
    j = MigrationJournal(tmp_path, doctype="Customer", source="s.csv")
    j.close()

    assert j.created is False
    assert j.count == 0
    assert not j.path.exists()
    assert list(tmp_path.glob("journal-*.jsonl")) == []
    assert j.summary() == "Nothing to undo: no effects were recorded."


def test_the_file_starts_at_the_first_effect_and_keeps_run_start_first(tmp_path):
    j = MigrationJournal(tmp_path, doctype="Item", source="s.csv")
    assert not j.path.exists()

    j.record_created("Item", "A")
    assert j.created is True and j.path.exists()

    j.record_created("Item", "B")
    j.close()

    events = [e["event"] for e in parse_journal(j.path)["effects"]]
    assert events == ["effect", "effect"]
    lines = [json.loads(l) for l in j.path.read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == ["run_start", "effect", "effect", "run_end"]
    assert lines[0]["source"] == "s.csv"        # the header is not lost by deferring
    assert lines[-1]["effects"] == 2 and lines[-1]["status"] == "ok"


def test_summary_reports_the_undo_path_once_there_are_effects(tmp_path):
    j = MigrationJournal(tmp_path, doctype="Item")
    j.record_created("Item", "A")
    j.close()
    assert j.summary().startswith(f"Journal: {j.path}")
    assert "(1 revertible effect(s))" in j.summary()


def test_closing_a_journal_without_effects_twice_is_safe(tmp_path):
    j = MigrationJournal(tmp_path, doctype="Item")
    j.close()
    j.close()                                   # must not raise, must not create a file
    assert not j.path.exists()


# ---------------------------------------------------- link effects (link-merge)
class _LinkClient:
    """Minimal client for the remove_record_link inverse."""

    def __init__(self, links):
        self.doc = {"name": "C1", "links": list(links)}
        self.updates: list = []

    def get(self, doctype, name):
        return dict(self.doc)

    def update(self, doctype, name, doc):
        self.updates.append(doc)
        self.doc.update(doc)
        return self.doc


def test_remove_record_link_drops_only_that_link():
    from erpgen.journal import apply_inverse

    client = _LinkClient([
        {"link_doctype": "Customer", "link_name": "Acme"},
        {"link_doctype": "Supplier", "link_name": "Acme"},
    ])
    ok, err = apply_inverse(client, {
        "op": "remove_record_link", "doctype": "Contact", "name": "C1",
        "link_doctype": "Supplier", "link_name": "Acme"})
    assert (ok, err) == (True, "")
    assert client.doc["links"] == [{"link_doctype": "Customer", "link_name": "Acme"}]


def test_remove_record_link_is_a_noop_when_already_unlinked():
    """Reverting twice must not write, and must not fail."""
    from erpgen.journal import apply_inverse

    client = _LinkClient([{"link_doctype": "Customer", "link_name": "Acme"}])
    ok, err = apply_inverse(client, {
        "op": "remove_record_link", "doctype": "Contact", "name": "C1",
        "link_doctype": "Supplier", "link_name": "Acme"})
    assert (ok, err) == (True, "")
    assert client.updates == [], "nothing to do => no write"


def test_describe_inverse_names_the_unlink():
    from erpgen.journal import describe_inverse

    text = describe_inverse({"op": "remove_record_link", "doctype": "Contact",
                             "name": "C1", "link_doctype": "Customer",
                             "link_name": "Acme"})
    assert "unlink Customer/Acme" in text and "Contact/C1" in text


def test_journal_records_a_link_add_with_its_inverse(tmp_path):
    from erpgen.journal import MigrationJournal

    j = MigrationJournal(tmp_path, doctype="Contact", source="s")
    j.link_added("Contact", "C1", "Supplier", "Acme")
    j.close()
    effect = [e for e in (json.loads(l) for l in j.path.read_text().splitlines())
              if e.get("event") == "effect"][0]
    assert effect["kind"] == "record_link_add"
    assert effect["inverse"] == {"op": "remove_record_link", "doctype": "Contact",
                                 "name": "C1", "link_doctype": "Supplier",
                                 "link_name": "Acme"}


def test_journal_close_accepts_a_status_like_the_run_context(tmp_path):
    """`_effect_sink` returns a journal or a context, so both closes must take the
    same arguments — the flat import crashed calling close(status=...) on a journal."""
    j = MigrationJournal(tmp_path, doctype="Customer")
    j.record_created("Customer", "Acme")
    j.close(status="ok")
    assert [e["event"] for e in parse_journal(j.path)["extra"]] == ["run_end"]
    assert parse_journal(j.path)["extra"][0]["status"] == "ok"


# ------------------------------------------------------- retention on disk
from erpgen.journal import KEEP_JOURNALS, prune_journals


def _seed(dirpath, index, effects=1, doctype="Item"):
    """A journal named the way the tool names them: the stamp carries creation order.

    Ordering must not depend on mtime, because reverting a journal appends a marker
    and therefore touches it.
    """
    p = Path(dirpath) / f"journal-{doctype.lower()}-20260101-{index:012d}.jsonl"
    lines = [json.dumps({"event": "run_start", "run_id": "x", "doctype": doctype})]
    lines += [json.dumps({"event": "effect", "kind": "record_create",
                          "inverse": {"op": "delete_record", "doctype": doctype,
                                      "name": f"X{i}"}}) for i in range(effects)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_prune_keeps_the_newest_and_deletes_older_empties(tmp_path):
    files = [_seed(tmp_path, i, effects=0) for i in range(25)]

    deleted, kept = prune_journals(tmp_path, keep=KEEP_JOURNALS)

    assert len(deleted) == 5 and kept == []
    assert not any(f.exists() for f in files[:5])          # the oldest five went
    assert all(f.exists() for f in files[5:])              # newest 20 kept


def test_prune_never_deletes_a_journal_with_un_reverted_effects(tmp_path):
    """That file IS the undo path for changes already applied."""
    live = [_seed(tmp_path, i, effects=1) for i in range(3)]        # oldest three
    rest = [_seed(tmp_path, i, effects=0) for i in range(3, 25)]

    deleted, kept = prune_journals(tmp_path, keep=KEEP_JOURNALS)

    assert all(f.exists() for f in live), "an un-reverted journal was destroyed"
    assert kept == sorted(live), "the leftovers must be reported, not hidden"
    assert len(deleted) == 2                                # only the safe overflow went
    assert len(list(tmp_path.glob("journal-*.jsonl"))) == 23


def test_prune_orders_by_the_stamp_not_mtime(tmp_path):
    """Reverting appends a marker, which touches mtime. Ordering by mtime would let
    an old reverted journal look newest and escape retention."""
    old = _seed(tmp_path, 0, effects=2)
    newer = _seed(tmp_path, 1, effects=0)
    mark_reverted(old, "x", 2, "ok")            # appends → mtime becomes "now"
    assert old.stat().st_mtime > newer.stat().st_mtime

    deleted, kept = prune_journals(tmp_path, keep=1)

    assert deleted == [old], "the reverted journal must still be seen as the oldest"
    assert kept == [] and newer.exists()


def test_prune_ignores_run_contexts(tmp_path):
    """`run-*.jsonl` holds requirements and a whole --run audit; not ours to prune."""
    ctx = tmp_path / "run-migration-01.jsonl"
    ctx.write_text('{"event": "run_start"}\n', encoding="utf-8")
    _seed(tmp_path, 1, effects=0)

    deleted, _kept = prune_journals(tmp_path, keep=1)

    assert ctx.exists() and deleted == []


def test_prune_never_deletes_the_open_journal(tmp_path):
    open_one = _seed(tmp_path, 0, effects=0)                # oldest: it is in overflow
    newest = _seed(tmp_path, 1, effects=0)

    deleted, _kept = prune_journals(tmp_path, keep=1, protect=open_one)

    assert open_one.exists() and deleted == [] and newest.exists()


def test_closing_a_journal_prunes_and_says_so(tmp_path):
    for i in range(KEEP_JOURNALS):
        _seed(tmp_path, i, effects=0)

    j = MigrationJournal(tmp_path, doctype="Item")          # stamp is "now" → newest
    j.record_created("Item", "A")
    j.close()

    assert len(list(tmp_path.glob("journal-*.jsonl"))) == KEEP_JOURNALS
    assert len(j.pruned) == 1                               # 21 files, cap 20
    assert j.retention_kept == []
    assert "[pruned 1 older journal(s)]" in j.summary()
    assert j.path.exists()


def test_summary_reports_when_the_cap_cannot_be_met(tmp_path):
    for i in range(3):                                      # oldest three stay live
        _seed(tmp_path, i, effects=1)
    for i in range(3, KEEP_JOURNALS):
        _seed(tmp_path, i, effects=0)

    j = MigrationJournal(tmp_path, doctype="Item")
    j.record_created("Item", "A")
    j.close()

    assert len(j.retention_kept) == 1                       # one live file in overflow
    assert "kept: their effects are not reverted yet" in j.summary()
