"""Tree sheets: a self-referencing sheet (Customer Group, Item Group, Supplier
Group) names its own parents, so

  * parent values it creates itself must not be reported as missing,
  * rows must import parents-first, and
  * an intermediate node must be a group.

ERPNext accepts a child-first insert order (it fails the row) and a leaf with
children (it does not fail at all), so neither is caught by the site.
"""
from __future__ import annotations

from conftest import make_field, make_meta, make_sheet
from erpgen.infer import guess_doctype
from erpgen.mapper import MappingEngine
from erpgen.tree import (apply_tree_semantics, derive_is_group, order_payloads,
                         self_link_field, sheet_identity_values,
                         without_self_provided)


def _cg_engine():
    """Customer Group: a name, a self-link parent, is_group, and the legacy
    `old_parent` self-link that must not be mistaken for the mapped one."""
    meta = make_meta("Customer Group", [
        make_field("customer_group_name", "Customer Group Name"),
        make_field("parent_customer_group", "Parent Customer Group", "Link",
                   options="Customer Group"),
        make_field("is_group", "Is Group", "Check"),
        make_field("old_parent", "old_parent", "Link", options="Customer Group"),
    ], autoname="field:customer_group_name")
    return MappingEngine(meta)


def _cg_plan(sheet):
    return _cg_engine(), sheet


def _sheet(rows, headers=("Customer Group Name", "Parent Customer Group",
                          "Is Group")):
    return make_sheet(list(headers), [list(r) for r in rows])


# ------------------------------------------------------------------ the signal
def test_self_link_field_follows_the_mapping_not_the_field_name():
    """`old_parent` is also a Link -> Customer Group; the mapped one wins."""
    engine = _cg_engine()
    sheet = _sheet([("Child", "Parent", "0")])
    plan = engine.suggest(sheet)
    assert self_link_field(plan, engine) == "parent_customer_group"


def test_a_flat_doctype_has_no_self_link():
    engine = MappingEngine(make_meta("Item", [
        make_field("item_code", "Item Code"),
        make_field("item_group", "Item Group", "Link", options="Item Group"),
    ], autoname="field:item_code"))
    plan = engine.suggest(make_sheet(["Item Code", "Item Group"], [["A", "B"]]))
    assert self_link_field(plan, engine) is None


# ------------------------------------------------------- self-provided parents
def test_a_self_referencing_sheets_own_parents_are_not_missing():
    """The observed false positive: 'All Wholesale' reported missing because
    the site has no such group yet — while the sheet itself creates it."""
    source = _sheet([("All Wholesale", "All Customer Groups", "1"),
                     ("Regional", "All Wholesale", "1")])
    engine = _cg_engine()
    plan = engine.suggest(source)

    assert sheet_identity_values(source, plan) == {"All Wholesale", "Regional"}
    assert without_self_provided(["All Wholesale"], "Customer Group",
                                 source, plan) == []


def test_a_parent_in_neither_the_sheet_nor_the_site_is_still_missing():
    source = _sheet([("All Wholesale", "All Customer Groups", "1")])
    engine = _cg_engine()
    plan = engine.suggest(source)
    assert without_self_provided(["Ghost Parent", "All Wholesale"],
                                 "Customer Group", source, plan) == ["Ghost Parent"]


def test_a_non_self_link_is_never_filtered():
    """A Customer's customer_group link points at another doctype: a
    coincidentally equal value must not be swallowed."""
    source = _sheet([("All Wholesale", "All Customer Groups", "1")])
    engine = _cg_engine()
    plan = engine.suggest(source)
    assert without_self_provided(["All Wholesale"], "Item Group",
                                 source, plan) == ["All Wholesale"]


# ------------------------------------------------------------------- ordering
def test_rows_are_ordered_parents_first():
    payloads = [
        {"customer_group_name": "North", "parent_customer_group": "Regional"},
        {"customer_group_name": "Regional", "parent_customer_group": "All Wholesale"},
        {"customer_group_name": "All Wholesale",
         "parent_customer_group": "All Customer Groups"},
    ]
    ordered, warnings = order_payloads(payloads, "customer_group_name",
                                       "parent_customer_group")
    assert [p["customer_group_name"] for p in ordered] == [
        "All Wholesale", "Regional", "North"]
    assert warnings == []


def test_a_parent_already_on_the_site_imposes_no_order():
    payloads = [
        {"customer_group_name": "A", "parent_customer_group": "Commercial"},
        {"customer_group_name": "B", "parent_customer_group": "Also Existing"},
    ]
    ordered, warnings = order_payloads(payloads, "customer_group_name",
                                       "parent_customer_group")
    assert [p["customer_group_name"] for p in ordered] == ["A", "B"]
    assert warnings == []


def test_a_cycle_keeps_source_order_and_warns():
    payloads = [
        {"customer_group_name": "A", "parent_customer_group": "B"},
        {"customer_group_name": "B", "parent_customer_group": "A"},
    ]
    ordered, warnings = order_payloads(payloads, "customer_group_name",
                                       "parent_customer_group")
    assert [p["customer_group_name"] for p in ordered] == ["A", "B"]
    assert len(warnings) == 1
    assert "cycle" in warnings[0]


def test_a_row_naming_itself_as_parent_is_not_an_infinite_loop():
    payloads = [{"customer_group_name": "A", "parent_customer_group": "A"}]
    ordered, warnings = order_payloads(payloads, "customer_group_name",
                                       "parent_customer_group")
    assert [p["customer_group_name"] for p in ordered] == ["A"]
    assert len(warnings) == 1


def test_ordering_keeps_every_row_and_never_puts_a_child_before_its_parent():
    payloads = [{"customer_group_name": n, "parent_customer_group": p}
                for n, p in [("C", "B"), ("B", "A"), ("A", "")]
                + [("X", "Ghost")]]
    ordered, _ = order_payloads(payloads, "customer_group_name",
                                "parent_customer_group")
    names = [p["customer_group_name"] for p in ordered]
    assert len(names) == 4
    for p in ordered:
        parent = p["parent_customer_group"]
        if parent in names:  # a parent outside the sheet imposes no order
            assert names.index(parent) < names.index(p["customer_group_name"])


# ------------------------------------------------------------------ is_group
def test_is_group_is_derived_from_the_parent_column():
    payloads = [
        {"customer_group_name": "All Wholesale",
         "parent_customer_group": "All Customer Groups"},
        {"customer_group_name": "Regional",
         "parent_customer_group": "All Wholesale"},
        {"customer_group_name": "North", "parent_customer_group": "Regional"},
    ]
    n = derive_is_group(payloads, "customer_group_name", "parent_customer_group")
    assert n == 2, "only the rows some other row names as a parent are groups"
    assert [p["is_group"] for p in payloads] == [1, 1, 0]


def test_a_source_is_group_column_is_never_overwritten():
    """The sheet's explicit answer wins over the derived one."""
    engine = _cg_engine()
    source = _sheet([("All Wholesale", "All Customer Groups", "1"),
                     ("Regional", "All Wholesale", "0")])
    plan = engine.suggest(source)
    payloads = engine.build_payloads(source, plan)[0]
    before = [dict(p) for p in payloads]

    _, warnings = apply_tree_semantics(engine, plan, payloads)

    assert [p["is_group"] for p in payloads] == [p["is_group"] for p in before]
    assert not any("is_group" in w for w in warnings)


# -------------------------------------------------------------- entry point
def test_a_tree_sheet_is_ordered_and_flagged_end_to_end():
    engine = _cg_engine()
    source = _sheet([("North", "Regional", ""),
                     ("Regional", "All Wholesale", ""),
                     ("All Wholesale", "All Customer Groups", "")])
    plan = engine.suggest(source)
    payloads = engine.build_payloads(source, plan)[0]

    ordered, warnings = apply_tree_semantics(engine, plan, payloads)

    assert [p["customer_group_name"] for p in ordered] == [
        "All Wholesale", "Regional", "North"]
    assert [p["is_group"] for p in ordered] == [1, 1, 0]
    assert any("is_group" in w for w in warnings)


def test_a_flat_sheet_is_left_alone():
    engine = MappingEngine(make_meta("Item", [
        make_field("item_code", "Item Code"),
        make_field("is_group", "Is Group", "Check"),
        make_field("item_group", "Item Group", "Link", options="Item Group"),
    ], autoname="field:item_code"))
    source = make_sheet(["Item Code", "Item Group"], [["A", "Products"]])
    plan = engine.suggest(source)
    payloads = engine.build_payloads(source, plan)[0]
    before = [dict(p) for p in payloads]

    ordered, warnings = apply_tree_semantics(engine, plan, payloads)

    assert ordered == before
    assert warnings == []


# ---------------------------------------------------------------- inference
def test_group_sheets_are_inferred_from_headers():
    assert guess_doctype(_sheet([("All Wholesale", "All Customer Groups", "1")])
                         ) == "Customer Group"
    assert guess_doctype(make_sheet(["Item Group Name", "Parent Item Group"],
                                    [["Products", "All Item Groups"]])) == "Item Group"
    assert guess_doctype(make_sheet(["Supplier Group Name"], [["Local"]])) == "Supplier Group"


def test_group_files_are_inferred_from_the_name():
    sheet = make_sheet(["Group", "Parent"], [["A", "B"]])
    sheet.name = "customer_groups.csv"
    assert guess_doctype(sheet) == "Customer Group"

    sheet.name = "supplier_groups.csv"
    assert guess_doctype(sheet) == "Supplier Group"

    sheet.name = "item_groups.csv"
    assert guess_doctype(sheet) == "Item Group"


def test_a_group_prefix_does_not_shadow_the_customer_file():
    """`customers.csv` must still be Customer, not Customer Group."""
    sheet = make_sheet(["Customer Name"], [["Acme"]])
    sheet.name = "customers.csv"
    assert guess_doctype(sheet) == "Customer"
