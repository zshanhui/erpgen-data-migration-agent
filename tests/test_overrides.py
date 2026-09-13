"""P0: the mapping-overrides file — load/save, set/unset, and applying decisions
on top of a scored plan (including the required-field recomputation).
"""
from __future__ import annotations

import json

import pytest

from conftest import make_field, make_meta, make_sheet
from erpgen.mapper import MappingEngine
from erpgen.overrides import (DEFAULT_OVERRIDES, apply_overrides, load_overrides,
                              save_overrides, set_mapping, unset_mapping)


# ------------------------------------------------------------- load / save
def test_load_overrides_missing_file_is_empty(tmp_path):
    assert load_overrides(tmp_path / "nope.json") == {}


def test_load_overrides_rejects_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        load_overrides(p)
    assert "not valid JSON" in str(e.value)


def test_load_overrides_error_names_the_line_and_how_to_recover(tmp_path):
    """Seen in the wild: stray trailing braces made the file unreadable and
    blocked the whole migration with only 'Extra data' to go on."""
    p = tmp_path / "bad.json"
    p.write_text('{\n  "Item": {}\n}\n}\n}\n', encoding="utf-8")
    with pytest.raises(ValueError) as e:
        load_overrides(p)
    msg = str(e.value)
    # note: the raw JSONDecodeError text already says "line 4 column 1", so assert
    # on our own formatted line specifically — otherwise this test never fails
    assert "at line 4, column 1" in msg, "the offending line must be named"
    assert "delete the file" in msg, "must say how to recover"


def test_save_overrides_writes_atomically_via_rename(tmp_path, monkeypatch):
    """A crash mid-write must not corrupt the file the next run reads."""
    import os as _os
    calls = []
    real_replace = _os.replace

    def _spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", _spy)
    p = tmp_path / "ov.json"
    save_overrides(p, {"Item": {"mappings": {"UoM": "stock_uom"}}})
    assert calls, "must publish via rename, not an in-place write"
    assert calls[0][1] == str(p)
    assert calls[0][0] != str(p), "must write a temp file first"


def test_save_overrides_leaves_no_temp_file_behind(tmp_path):
    p = tmp_path / "ov.json"
    save_overrides(p, {"Item": {"mappings": {"UoM": "stock_uom"}}})
    assert sorted(f.name for f in tmp_path.iterdir()) == ["ov.json"]


def test_save_overrides_replaces_an_existing_file_completely(tmp_path):
    """No stale tail from the previous, longer content."""
    p = tmp_path / "ov.json"
    save_overrides(p, {"Item": {"mappings": {"A": "a", "B": "b"}}})
    save_overrides(p, {"Item": {"mappings": {"C": "c"}}})
    assert load_overrides(p) == {"Item": {"mappings": {"C": "c"}}}
    assert '"A"' not in p.read_text(encoding="utf-8")


def test_save_and_load_round_trip_unicode(tmp_path):
    p = tmp_path / "ov.json"
    data = {"Item": {"mappings": {"Größe": "size"}, "defaults": {}, "value_maps": {}}}
    save_overrides(p, data)
    assert load_overrides(p) == data
    assert "Größe" in p.read_text(encoding="utf-8"), "must not be ASCII-escaped"


def test_default_overrides_path_is_relative():
    assert not DEFAULT_OVERRIDES.startswith("/")


# ------------------------------------------------- concurrency (parallel tool calls)
def test_save_overrides_gives_each_writer_its_own_temp_file(tmp_path, monkeypatch):
    """Seen in the wild: two `set_mapping` calls in one LLM turn share a temp
    path, so one rename takes it away (ENOENT) or both interleave into it and
    publish invalid JSON — which then blocks the whole migration."""
    import os as _os
    import threading

    p = tmp_path / "ov.json"
    at_publish = threading.Barrier(2, timeout=5)
    srcs, errors = [], []
    real_replace = _os.replace

    def _replace(src, dst):
        srcs.append(str(src))
        at_publish.wait()          # both writers must be at the publish step
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", _replace)

    def writer(col):
        try:
            save_overrides(p, {"Item": {"mappings": {col: col}}})
        except Exception as e:  # noqa: BLE001 — surfaced through `errors`
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(c,)) for c in ("A", "B")]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(set(srcs)) == 2, f"writers shared a temp file: {srcs}"
    assert not errors, f"a concurrent save failed: {errors}"
    assert load_overrides(p)["Item"]["mappings"] in ({"A": "A"}, {"B": "B"})


def test_concurrent_set_mapping_keeps_both_decisions(tmp_path, monkeypatch):
    """load -> modify -> save must not interleave, or the second writer
    publishes a snapshot taken before the first decision landed. That is how
    the agent silently lost an override it had already recorded."""
    import threading
    import time

    import erpgen.overrides as ov

    p = tmp_path / "ov.json"
    real_load = ov.load_overrides

    def slow_load(path):
        data = real_load(path)
        time.sleep(0.2)            # widen the window both writers must be inside
        return data

    monkeypatch.setattr(ov, "load_overrides", slow_load)
    start = threading.Barrier(2, timeout=5)

    def writer(col, target):
        start.wait()
        ov.set_mapping(p, "Item", col, target)

    threads = [threading.Thread(target=writer, args=("Group", "item_group")),
               threading.Thread(target=writer, args=("Rate", "standard_rate"))]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert load_overrides(p)["Item"]["mappings"] == {"Group": "item_group",
                                                     "Rate": "standard_rate"}


# ------------------------------------------------------------- set / unset
def test_set_mapping_creates_a_full_block(tmp_path):
    p = tmp_path / "ov.json"
    block = set_mapping(p, "Item", "Group", "item_group")
    assert block == {"mappings": {"Group": "item_group"}, "defaults": {}, "value_maps": {}}
    assert load_overrides(p)["Item"]["mappings"]["Group"] == "item_group"


def test_set_mapping_overwrites_and_keeps_siblings(tmp_path):
    p = tmp_path / "ov.json"
    set_mapping(p, "Item", "Group", "item_group")
    set_mapping(p, "Item", "UoM", "stock_uom")
    set_mapping(p, "Item", "Group", "item_group_fk")
    assert load_overrides(p)["Item"]["mappings"] == {"Group": "item_group_fk",
                                                     "UoM": "stock_uom"}


def test_set_mapping_preserves_other_doctypes_and_sections(tmp_path):
    p = tmp_path / "ov.json"
    save_overrides(p, {"Customer": {"mappings": {"Website": "customer.website"},
                                    "defaults": {"customer_type": "Company"},
                                    "value_maps": {"Group": {"A": "B"}}}})
    set_mapping(p, "Item", "Group", "item_group")
    data = load_overrides(p)
    assert data["Customer"]["defaults"] == {"customer_type": "Company"}
    assert data["Customer"]["value_maps"] == {"Group": {"A": "B"}}
    assert data["Item"]["mappings"] == {"Group": "item_group"}


def test_unset_mapping_reports_whether_it_removed_anything(tmp_path):
    p = tmp_path / "ov.json"
    set_mapping(p, "Item", "Group", "item_group")
    set_mapping(p, "Item", "UoM", "stock_uom")
    assert unset_mapping(p, "Item", "Group") is True
    assert load_overrides(p)["Item"]["mappings"] == {"UoM": "stock_uom"}
    assert unset_mapping(p, "Item", "Group") is False       # already gone
    assert unset_mapping(p, "Customer", "Group") is False   # unknown doctype
    assert load_overrides(p)["Item"]["mappings"] == {"UoM": "stock_uom"}


def test_unset_mapping_keeps_an_empty_block(tmp_path):
    """set/unset round-trips must not corrupt the file structure."""
    p = tmp_path / "ov.json"
    set_mapping(p, "Item", "Group", "item_group")
    unset_mapping(p, "Item", "Group")
    assert load_overrides(p) == {"Item": {"mappings": {}, "defaults": {},
                                          "value_maps": {}}}
    assert json.loads(p.read_text(encoding="utf-8"))  # still valid JSON


# ------------------------------------------------------- applying overrides
def _engine_with_mandatory():
    """A doctype with one mandatory field and a decoy for the forced mapping."""
    meta = make_meta("Item", [
        make_field("item_name", "Item Name", reqd=True),
        make_field("item_group", "Item Group", "Link", options="Item Group"),
        make_field("grp_legacy", "GRP Legacy"),
        make_field("description", "Description"),
    ])
    return MappingEngine(meta), make_sheet(["Item Name", "Group"], [["A", "x"]])


def test_apply_overrides_forces_the_target_and_clears_ambiguity():
    engine, sheet = _engine_with_mandatory()
    plan = engine.suggest(sheet)
    m = next(x for x in plan.mappings if x.source == "Group")
    m.alternatives = ["grp_legacy"]
    m.notes.append("ambiguous with: grp_legacy")

    n = apply_overrides(plan, sheet, engine,
                        {"mappings": {"Group": "item_group"}})

    assert n == 1
    assert m.target == "item_group"
    assert m.confidence == 1.0
    assert m.method == "override"
    assert m.alternatives == []
    assert not any("ambiguous with" in note for note in m.notes)


def test_apply_overrides_ignores_unknown_source_column():
    engine, sheet = _engine_with_mandatory()
    plan = engine.suggest(sheet)
    before = [m.target for m in plan.mappings]
    n = apply_overrides(plan, sheet, engine, {"mappings": {"Nope": "item_name"}})
    assert n == 0
    assert [m.target for m in plan.mappings] == before
    assert any("unknown source column" in w for w in plan.warnings)


def test_apply_overrides_ignores_unknown_target():
    engine, sheet = _engine_with_mandatory()
    plan = engine.suggest(sheet)
    n = apply_overrides(plan, sheet, engine, {"mappings": {"Group": "does_not_exist"}})
    assert n == 0
    assert any("not found on Item" in w for w in plan.warnings)


def test_apply_overrides_can_target_a_child_table_field():
    parent = make_meta("Item", [
        make_field("item_name", "Item Name", reqd=True),
        make_field("credit_limits", "Credit Limits", "Table", options="Credit Limit"),
    ])
    child = make_meta("Credit Limit", [make_field("credit_limit", "Credit Limit")],
                      istable=True)
    engine = MappingEngine(parent, {"Credit Limit": child})
    sheet = make_sheet(["Limit"], [["100"]])
    plan = engine.suggest(sheet)
    n = apply_overrides(plan, sheet, engine,
                        {"mappings": {"Limit": "credit_limits.credit_limit"}})
    assert n == 1
    m = next(x for x in plan.mappings if x.source == "Limit")
    assert m.target == "credit_limits.credit_limit"


def test_apply_overrides_merges_defaults_and_value_maps():
    engine, sheet = _engine_with_mandatory()
    plan = engine.suggest(sheet)
    apply_overrides(plan, sheet, engine, {
        "defaults": {"item_group": "Products"},
        "value_maps": {"Group": {"Wholesale": "Commercial"}},
    })
    assert plan.defaults["item_group"] == "Products"
    assert plan.value_maps == {"Group": {"Wholesale": "Commercial"}}


def test_apply_overrides_recomputes_required_field_coverage():
    """The regression fixed by hand: an override satisfies a mandatory field, so
    the 'has no source column and no default' warning must disappear."""
    # `stock_uom` is deliberately *not* the mandatory field here: the synonym
    # table already maps a "UOM" column onto it, which would mask what we test.
    meta = make_meta("Item", [
        make_field("item_name", "Item Name", reqd=True),
        make_field("extra_ref", "Extra Ref", reqd=True),
    ])
    engine = MappingEngine(meta)
    sheet = make_sheet(["Item Name", "Legacy Code"], [["A", "L-1"]])

    plan = engine.suggest(sheet)
    assert any("Required field 'extra_ref'" in w for w in plan.warnings), plan.warnings

    apply_overrides(plan, sheet, engine, {"mappings": {"Legacy Code": "extra_ref"}})
    assert not any("Required field 'extra_ref'" in w for w in plan.warnings)


def test_apply_overrides_recomputes_coverage_when_a_default_fills_it():
    meta = make_meta("Item", [make_field("stock_uom", "Stock UOM", reqd=True)])
    engine = MappingEngine(meta)
    sheet = make_sheet(["Nothing"], [["x"]])
    plan = engine.suggest(sheet)
    assert any("Required field 'stock_uom'" in w for w in plan.warnings)
    apply_overrides(plan, sheet, engine, {"defaults": {"stock_uom": "Nos"}})
    assert not any("Required field 'stock_uom'" in w for w in plan.warnings)


def test_apply_overrides_does_not_duplicate_coverage_warnings():
    meta = make_meta("Item", [make_field("stock_uom", "Stock UOM", reqd=True)])
    engine = MappingEngine(meta)
    sheet = make_sheet(["Item Name"], [["A"]])
    plan = engine.suggest(sheet)
    apply_overrides(plan, sheet, engine, {"mappings": {}})
    apply_overrides(plan, sheet, engine, {"mappings": {}})
    assert sum("Required field 'stock_uom'" in w for w in plan.warnings) == 1


def test_apply_overrides_returns_zero_for_an_empty_block():
    engine, sheet = _engine_with_mandatory()
    plan = engine.suggest(sheet)
    assert apply_overrides(plan, sheet, engine, {}) == 0
