"""A Link value that exists but is a *Group* node where ERPNext requires a leaf.

ERPNext validates exactly one field this way — `Customer.customer_group` — and it
throws rather than coercing: "Cannot select a Group type Customer Group"
(HTTP 417). Every row then fails mid-import with a raw traceback, so it has to be
reported up front.

It must NOT be reported as a missing value: the record is right there, and
"create the missing record" is exactly the fix that would loop the agent forever.
An Item or a Customer happily takes a group node in `item_group` / `territory`,
so the check is deliberately confined to `LEAF_ONLY_LINKS`.

Pure: no network. `_Site` only implements `list`.
"""
from __future__ import annotations

from conftest import make_field, make_meta, make_sheet
from erpgen.analysis import build_analysis
from erpgen.client import ERPNextError
from erpgen.conflicts import (LEAF_ONLY_LINKS, group_node_values,
                              link_group_node, missing_link_values)
from erpgen.mapper import MappingEngine


class _Site:
    """`list` only — the name check asks for names, the leaf check for is_group."""

    def __init__(self, docs):
        self.docs = docs              # {doctype: {name: {field: value}}}
        self.queries: list[str] = []

    def list(self, doctype, filters=None, fields=None, limit=0, order_by=None):
        self.queries.append(doctype)
        if doctype not in self.docs:
            raise ERPNextError(f"HTTP 404 GET /api/resource/{doctype}")
        return [dict({"name": n}, **d) for n, d in self.docs[doctype].items()]


_ALL_GROUPS = {"name": "All Customer Groups", "is_group": 1}
_COMMERCIAL = {"name": "Commercial", "is_group": 0}


def _site(groups, territory_is_group=0):
    return _Site({
        "Customer Group": groups,
        "Territory": {"All Territories": {"name": "All Territories",
                                          "is_group": territory_is_group}},
    })


def _customer_engine():
    return MappingEngine(make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True),
        make_field("customer_type", "Customer Type"),
        make_field("customer_group", "Group", "Link", options="Customer Group"),
        make_field("territory", "Territory", "Link", options="Territory"),
    ]))


def _analyse(site, rows, headers=("Customer Name", "Customer Type", "Group",
                                  "Territory")):
    engine = _customer_engine()
    sheet = make_sheet(list(headers), [list(r) for r in rows])
    plan = engine.suggest(sheet)
    return build_analysis(site, sheet, plan, engine, id_column="Customer Name")


def _kinds(analysis, kind):
    return [c for c in analysis["conflicts"] if c["kind"] == kind]


# ------------------------------------------------------------------ the helper
def test_group_node_values_lists_only_group_nodes():
    site = _site({"All Customer Groups": _ALL_GROUPS, "Commercial": _COMMERCIAL})
    assert group_node_values(site, ["All Customer Groups", "Commercial"],
                             "Customer Group") == ["All Customer Groups"]


def test_group_node_values_is_silent_when_the_doctype_cannot_be_queried():
    site = _Site({})                      # 404s for everything
    assert group_node_values(site, ["Whatever"], "Customer Group") is None


def test_group_node_values_short_circuits_without_values():
    site = _Site({})
    assert group_node_values(site, [], "Customer Group") == []
    assert site.queries == [], "must not query for an empty column"


def test_the_two_lookups_can_share_one_cache_without_colliding():
    """Both memoise by doctype. Sharing one key would make the name lookup read a
    set of group nodes as if it were every name (or the reverse) — one of the two
    answers then silently inverts."""
    site = _site({"All Customer Groups": _ALL_GROUPS, "Commercial": _COMMERCIAL})
    cache: dict = {}

    assert missing_link_values(site, ["All Customer Groups", "Nope"],
                               "Customer Group", cache) == ["Nope"]
    assert group_node_values(site, ["All Customer Groups", "Commercial"],
                             "Customer Group", cache) == ["All Customer Groups"]

    # and again, now served from the cache — the answers must not drift
    assert missing_link_values(site, ["Commercial"], "Customer Group", cache) == []
    assert group_node_values(site, ["Commercial"], "Customer Group", cache) == []


# ------------------------------------------------------------------ the conflict
def test_a_group_node_is_reported_as_its_own_kind():
    a = _analyse(_site({"All Customer Groups": _ALL_GROUPS}),
                 [["Acme", "Company", "All Customer Groups", "All Territories"]])
    got = _kinds(a, "link_group_node")
    assert len(got) == 1
    c = got[0]
    assert c["kind"] == "link_group_node"
    assert c["severity"] == "error"
    assert c["doctype"] == "Customer Group"
    assert c["source"] == "Group"
    assert c["target"] == "customer_group"
    assert c["group_values"] == ["All Customer Groups"]


def test_a_group_node_is_not_also_reported_as_missing():
    a = _analyse(_site({"All Customer Groups": _ALL_GROUPS}),
                 [["Acme", "Company", "All Customer Groups", "All Territories"]])
    assert _kinds(a, "link_value_conflict") == []


def test_a_leaf_group_is_clean():
    a = _analyse(_site({"Commercial": _COMMERCIAL}),
                 [["Acme", "Company", "Commercial", "All Territories"]])
    assert a["conflicts"] == []


def test_a_genuinely_missing_group_is_still_a_missing_value():
    """Only the *existing but unusable* case is link_group_node."""
    a = _analyse(_site({"Commercial": _COMMERCIAL}),
                 [["Acme", "Company", "Nonexistent", "All Territories"]])
    assert _kinds(a, "link_group_node") == []
    assert _kinds(a, "link_value_conflict")[0]["missing_values"] == ["Nonexistent"]


def test_only_the_group_node_values_are_listed():
    a = _analyse(_site({"All Customer Groups": _ALL_GROUPS,
                        "Commercial": _COMMERCIAL}),
                 [["Acme", "Company", "Commercial", "All Territories"],
                  ["Beta", "Company", "All Customer Groups", "All Territories"]])
    c = _kinds(a, "link_group_node")[0]
    assert c["group_values"] == ["All Customer Groups"]


# ------------------------------------------------- the false-positive guards
def test_a_group_territory_is_not_reported():
    """`Customer.territory` accepts a group node — ERPNext has no check for it."""
    a = _analyse(_site({"Commercial": _COMMERCIAL}, territory_is_group=1),
                 [["Acme", "Company", "Commercial", "All Territories"]])
    assert _kinds(a, "link_group_node") == []
    assert a["conflicts"] == []


def test_leaf_only_links_are_a_deliberately_narrow_list():
    assert LEAF_ONLY_LINKS == {("Customer", "customer_group")}


# ------------------------------------------------------------------ the shape
def test_the_conflict_never_asks_for_the_record_to_be_created_again():
    c = link_group_node("Group", ["customer_group"], "Customer Group",
                        ["All Customer Groups"])
    assert "missing" not in c["suggested_action"]
    assert "leaf" in c["suggested_action"]
    # the reachable verb, not the `value_map` the tools do not have
    assert "set_value" in c["suggested_action"]
    assert "value_map" not in c["suggested_action"]
