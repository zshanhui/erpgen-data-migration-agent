"""P0: the reactive-coeffect side of MigrationContext.

Requirements are what the migration *needs*. These tests pin the behaviour that
was repeatedly broken by hand-testing: cross-command visibility, sequenced ids,
identity de-duplication, partial coverage, condition-vs-effect satisfaction, and
regression reopening.
"""
from __future__ import annotations

import json

from erpgen.context import load_run

# --------------------------------------------------------------- fixtures
LINK = {
    "kind": "link_value_conflict", "severity": "error", "source": "UoM",
    "target": "stock_uom", "doctype": "UOM", "missing_values": ["Dozen", "Roll"],
    "detail": "2 source value(s) for 'stock_uom' do not exist in UOM.",
}
UNMAPPED = {
    "kind": "unmapped_column", "severity": "info", "source": "Notes",
    "detail": "Source column has no matching ERPNext field; values are dropped.",
}
AMBIG = {
    "kind": "ambiguous_mapping", "severity": "warning", "source": "Group",
    "target": "item_group", "alternatives": ["customer_items.customer_group"],
    "detail": "'Group' scores equally for item_group, customer_items.customer_group.",
}


def _pending_ids(ctx) -> set[str]:
    return {r["id"] for r in ctx.pending_requirements()}


# ----------------------------------------------------- cross-command state
def test_requirements_are_visible_to_a_later_command(mkctx):
    """A requirement raised by `map` must be satisfiable by a later command."""
    c1 = mkctx("run1")
    assert c1.add_requirements([LINK]) == 1
    c1.close()

    c2 = mkctx("run1")
    assert len(c2.pending_requirements()) == 1, "requirement lost on reopen"

    c2.record_created("UOM", "Dozen")
    assert len(c2.pending_requirements()) == 1, "closed on partial coverage"
    c2.record_created("UOM", "Roll")
    assert c2.pending_requirements() == []


def test_requirement_ids_continue_across_commands(mkctx):
    c1 = mkctx("run1")
    c1.add_requirements([UNMAPPED])
    c1.close()
    c2 = mkctx("run1")
    c2.add_requirements([{"kind": "required_missing", "severity": "error",
                          "field": "item_group",
                          "detail": "Required field 'item_group' has no source column."}])
    ids = sorted(_pending_ids(c2))
    assert len(ids) == 2
    assert ids[0].startswith("r1-") and ids[1].startswith("r2-"), ids


def test_effects_and_requirements_share_one_file(mkctx, tmp_path):
    c = mkctx("run1")
    c.add_requirements([LINK])
    c.record_created("UOM", "Dozen")
    c.close()
    assert c.path == tmp_path / "run-run1.jsonl"
    data = load_run("run1", tmp_path)
    assert len(data["requirements"]) == 1
    assert len(data["effects"]) == 1


# ------------------------------------------------------- identity dedup
def test_identical_requirement_is_not_recorded_twice(mkctx):
    """`map` and `import` both re-run the mapper; the same conflict must not
    become a new requirement each time."""
    c = mkctx("run1")
    assert c.add_requirements([UNMAPPED]) == 1
    assert c.add_requirements([UNMAPPED]) == 0
    assert c.add_requirements([dict(UNMAPPED)]) == 0
    assert len(load_run("run1", c.path.parent)["requirements"]) == 1


def test_distinct_requirements_are_both_kept(mkctx):
    c = mkctx("run1")
    other = {"kind": "unmapped_column", "severity": "info", "source": "Internal Ref"}
    assert c.add_requirements([UNMAPPED, other]) == 2
    assert len(_pending_ids(c)) == 2


def test_satisfied_requirement_reopens_when_it_reappears(mkctx):
    """If a fix is undone, the requirement must come back — not stay 'satisfied'."""
    c = mkctx("run1")
    c.add_requirements([AMBIG])
    c.override_set("Item", "Group", "item_group", None, "ov.json")
    assert c.pending_requirements() == []
    c.close()

    c2 = mkctx("run1")
    assert c2.add_requirements([AMBIG]) == 1, "regression not detected"
    reqs = load_run("run1", c2.path.parent)["requirements"]
    assert len(reqs) == 2
    reopened = [r for r in reqs if r.get("reopened")]
    assert len(reopened) == 1
    assert reopened[0]["satisfied_by"] is None


def test_satisfaction_event_is_logged_once(mkctx):
    c = mkctx("run1")
    c.add_requirements([LINK])
    c.record_created("UOM", "Dozen")
    c.record_created("UOM", "Roll")
    c.close()
    # reopening replays the effects; it must not re-log the satisfaction
    mkctx("run1").close()
    lines = c.path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l)["event"] for l in lines if l.strip()]
    assert events.count("requirement_satisfied") == 1


# ------------------------------------------------------- reactive matching
def test_unmapped_column_satisfied_by_custom_field_label(mkctx):
    c = mkctx("run1")
    c.add_requirements([UNMAPPED])
    c.custom_field_created("Item", "notes", "Item-notes", label="Notes")
    assert c.pending_requirements() == []


def test_unmapped_column_matches_fieldname_case_insensitively(mkctx):
    """`create_field` without a label journals the derived fieldname."""
    c = mkctx("run1")
    c.add_requirements([UNMAPPED])
    c.custom_field_created("Item", "notes", "Item-notes")  # label defaults to fieldname
    assert c.pending_requirements() == []


def test_unmapped_column_not_satisfied_by_an_unrelated_field(mkctx):
    c = mkctx("run1")
    c.add_requirements([UNMAPPED])
    c.custom_field_created("Item", "vendor_code", "Item-vendor_code", label="Vendor Code")
    assert len(c.pending_requirements()) == 1


def test_link_requirement_requires_every_missing_value(mkctx):
    c = mkctx("run1")
    c.add_requirements([LINK])
    c.record_created("UOM", "Dozen")
    assert len(c.pending_requirements()) == 1
    c.record_created("UOM", "Roll")
    assert c.pending_requirements() == []


def test_link_requirement_ignores_other_doctypes_and_values(mkctx):
    c = mkctx("run1")
    c.add_requirements([LINK])
    c.record_created("UOM", "Set")                 # right doctype, wrong value
    c.record_created("Item Group", "Dozen")        # right value, wrong doctype
    assert len(c.pending_requirements()) == 1


def test_ambiguous_mapping_cleared_by_override_on_source_column(mkctx):
    c = mkctx("run1")
    c.add_requirements([AMBIG])
    c.override_set("Item", "Rate", "standard_rate", None, "ov.json")  # other column
    assert len(c.pending_requirements()) == 1
    c.override_set("Item", "Group", "item_group", None, "ov.json")
    assert c.pending_requirements() == []


def test_ambiguous_mapping_respects_declared_doctype(mkctx):
    c = mkctx("run1")
    c.add_requirements([{**AMBIG, "doctype": "Item"}])
    c.override_set("Customer", "Group", "customer_group", None, "ov.json")
    assert len(c.pending_requirements()) == 1, "matched the wrong doctype"
    c.override_set("Item", "Group", "item_group", None, "ov.json")
    assert c.pending_requirements() == []


# ------------------------------------------------- condition (no effect)
def test_condition_satisfies_requirement_without_journaling_an_effect(mkctx):
    """A pre-existing record satisfies the requirement but must not be owned by
    the run — otherwise `revert` would delete data it never created."""
    c = mkctx("run1")
    c.add_requirements([LINK])
    c.note_condition("record_create", doctype="UOM", name="Dozen")
    c.note_condition("record_create", doctype="UOM", name="Roll")
    assert c.pending_requirements() == []
    assert c.effects == 0, "a condition must not create an undoable effect"
    c.close()

    reqs = load_run("run1", c.path.parent)["requirements"]
    assert reqs[0]["via"] == "condition"
    assert load_run("run1", c.path.parent)["effects"] == []


def test_condition_does_not_satisfy_an_unrelated_requirement(mkctx):
    c = mkctx("run1")
    c.add_requirements([UNMAPPED])
    c.note_condition("record_create", doctype="UOM", name="Notes")
    assert len(c.pending_requirements()) == 1


def test_effect_satisfaction_is_recorded_as_via_effect(mkctx):
    c = mkctx("run1")
    c.add_requirements([LINK])
    c.record_created("UOM", "Dozen")
    c.record_created("UOM", "Roll")
    c.close()
    reqs = load_run("run1", c.path.parent)["requirements"]
    assert reqs[0]["via"] == "effect"
    assert reqs[0]["satisfied_by"] == 2  # the seq of the closing effect
