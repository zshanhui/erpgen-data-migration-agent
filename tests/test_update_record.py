"""P0: `update_record` — the one write path onto a record that already exists.

Pure: a FakeClient stands in for the site, and the journal is a real file in
`tmp_path`. The point of these tests is the inverse: an update that cannot be
undone would be the first mutation in this codebase that `revert` cannot replay.
"""
from __future__ import annotations

import json

import pytest

from conftest import FakeClient
from erpgen import tools as erpgen_tools
from erpgen.journal import (MigrationJournal, apply_inverse, describe_inverse,
                            parse_journal, revert_journal)
from erpgen.tools import update_record


def _site():
    return FakeClient(existing={("Customer Group", "Wholesale")},
                      docs={("Customer Group", "Wholesale"):
                            {"name": "Wholesale", "is_group": 0,
                             "parent_customer_group": "All Customer Groups"}})


# ------------------------------------------------------------------- the write
def test_update_writes_the_field_and_reports_what_changed():
    client = _site()
    result = update_record(client, "Customer Group", "Wholesale", {"is_group": 1})

    assert result == {"name": "Wholesale", "updated": ["is_group"]}
    assert client.docs[("Customer Group", "Wholesale")]["is_group"] == 1
    assert [c[0] for c in client.calls] == ["get", "update"], \
        "the read must come first: it is what the inverse is built from"


def test_update_refuses_to_rename():
    """`name` is a PUT field on Frappe, but renaming moves every link with it."""
    with pytest.raises(ValueError, match="cannot rename"):
        update_record(_site(), "Customer Group", "Wholesale", {"name": "Retail"})


def test_update_needs_something_to_write():
    with pytest.raises(ValueError, match="at least one field"):
        update_record(_site(), "Customer Group", "Wholesale", {})


def test_update_on_a_missing_record_fails_before_writing():
    client = _site()
    with pytest.raises(Exception):
        update_record(client, "Customer Group", "Nope", {"is_group": 1})
    assert [c[0] for c in client.calls] == ["get"], "no write after a failed read"


# ---------------------------------------------------------------- the inverse
def test_the_effect_records_the_previous_values(tmp_path):
    client = _site()
    journal = MigrationJournal(tmp_path, doctype="Customer Group")
    erpgen_tools.ACTIVE_JOURNAL = journal
    try:
        update_record(client, "Customer Group", "Wholesale", {"is_group": 1})
    finally:
        erpgen_tools.ACTIVE_JOURNAL = None
        journal.close()

    [effect] = parse_journal(journal.path)["effects"]
    assert effect["kind"] == "record_update"
    assert effect["inverse"] == {
        "op": "restore_record", "doctype": "Customer Group", "name": "Wholesale",
        "fields": {"is_group": 0},          # only what was written is captured
    }


def test_revert_puts_the_old_value_back(tmp_path):
    client = _site()
    journal = MigrationJournal(tmp_path, doctype="Customer Group")
    erpgen_tools.ACTIVE_JOURNAL = journal
    try:
        update_record(client, "Customer Group", "Wholesale", {"is_group": 1})
    finally:
        erpgen_tools.ACTIVE_JOURNAL = None
        journal.close()
    assert client.docs[("Customer Group", "Wholesale")]["is_group"] == 1

    outcome = revert_journal(client, journal.path, apply=True)

    assert outcome["failed"] == []
    assert outcome["applied"] == 1
    assert outcome["results"][0]["description"] == \
        "restore Customer Group/Wholesale is_group"
    assert client.docs[("Customer Group", "Wholesale")]["is_group"] == 0
    assert client.docs[("Customer Group", "Wholesale")][
        "parent_customer_group"] == "All Customer Groups", \
        "revert restores the written cells, not the whole document"


def test_no_journal_set_means_no_effect_but_the_write_still_happens():
    """A bare library call (the CLI passes a context) must not need a sink."""
    client = _site()
    erpgen_tools.ACTIVE_JOURNAL = None
    update_record(client, "Customer Group", "Wholesale", {"is_group": 1})
    assert client.docs[("Customer Group", "Wholesale")]["is_group"] == 1


def test_the_run_context_journals_the_same_inverse(tmp_path):
    """With `--run`, effects land in the shared context instead of a per-command
    journal. The context has to record the update too, or `revert <run-id>`
    silently leaves the field change behind."""
    from erpgen.context import MigrationContext, load_run

    client = _site()
    ctx = MigrationContext("run-upd", tmp_path, source="samples/customers.csv",
                           doctypes=["Customer Group"])
    erpgen_tools.ACTIVE_JOURNAL = ctx
    try:
        update_record(client, "Customer Group", "Wholesale", {"is_group": 1})
    finally:
        erpgen_tools.ACTIVE_JOURNAL = None
        ctx.close()

    [effect] = load_run("run-upd", tmp_path)["effects"]
    assert effect["kind"] == "record_update"
    assert effect["inverse"] == {"op": "restore_record",
                                 "doctype": "Customer Group", "name": "Wholesale",
                                 "fields": {"is_group": 0}}


# ----------------------------------------------------------- inverse mechanics
def test_restore_record_dry_run_does_not_touch_the_client(fake_client):
    inv = {"op": "restore_record", "doctype": "Customer Group", "name": "Wholesale",
           "fields": {"is_group": 0}}
    assert apply_inverse(fake_client, inv, apply=False) == (True, "")
    assert fake_client.calls == []


def test_restore_record_writes_the_recorded_fields():
    client = _site()
    inv = {"op": "restore_record", "doctype": "Customer Group", "name": "Wholesale",
           "fields": {"is_group": 0}}
    ok, err = apply_inverse(client, inv)
    assert (ok, err) == (True, "")
    assert client.calls == [("update", "Customer Group", "Wholesale")]


def test_restore_record_with_nothing_recorded_is_a_no_op():
    client = _site()
    ok, err = apply_inverse(client, {"op": "restore_record",
                                     "doctype": "Customer Group",
                                     "name": "Wholesale", "fields": {}})
    assert (ok, err) == (True, "")
    assert client.calls == []


def test_describe_inverse_names_the_record_and_fields():
    text = describe_inverse({"op": "restore_record", "doctype": "Customer Group",
                             "name": "Wholesale", "fields": {"is_group": 0}})
    assert text == "restore Customer Group/Wholesale is_group"


# ------------------------------------------------------------------ the tool
def test_the_agent_tool_returns_json_and_surfaces_errors(agent_mod, monkeypatch):
    from erpgen.agent import tools as agent_tools

    client = _site()
    monkeypatch.setattr(agent_tools, "CLIENT", client)

    out = json.loads(agent_tools.t_update_record(
        "Customer Group", "Wholesale", '{"is_group": 1}'))
    assert out["updated"] == ["is_group"]
    assert client.docs[("Customer Group", "Wholesale")]["is_group"] == 1

    bad = json.loads(agent_tools.t_update_record(
        "Customer Group", "Nope", '{"is_group": 1}'))
    assert "404" in bad["error"]

    not_json = json.loads(agent_tools.t_update_record("Customer Group", "X", "{"))
    assert "error" in not_json, "bad JSON comes back as an error, never a raise"


def test_the_tool_is_registered_and_the_playbook_knows_it(agent_mod):
    names = {t["name"] for t in agent_mod.TOOLS}
    assert "update_record" in names
    assert "update_record" in agent_mod.SYSTEM_PROMPT
