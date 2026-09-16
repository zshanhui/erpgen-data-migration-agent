"""The queries we send the site have to be the queries we mean.

Every "does this already exist?" decision the importer makes is answered by a
`client.list(filters=...)` call, so a filter that can never match looks exactly
like "nothing there yet" — and the row is created a second time. The suite could
not tell those apart: `FakeClient.list` ignored `filters` altogether, so poisoning
both of the importer's key lookups (wrong field, wrong value) left every test
green.

These assert the *outcome* of the query rather than its shape: a record the site
holds must be found through the field it is actually stored under. Each test here
is the target of a `scripts/mutation-check.py` entry, so the filter cannot quietly
rot back.
"""
from __future__ import annotations

import pytest

from conftest import FakeClient
from erpgen.dedup import dedup_key_label, dedup_payloads, existing_keys, existing_names
from erpgen.mapper import MappingPlan
from erpgen.tools import create_record, get_record, list_records

#: `name` and the natural key differ on purpose: `create_record` must look the
#: record up through the autoname field (`item_group_name`), and a lookup that
#: filters `name` instead (the bug these guard) finds nothing.
ITEM_GROUP_META = {
    "name": "Item Group",
    "autoname": "field:item_group_name",
    "istable": 0,
    "is_submittable": 0,
    "fields": [
        {"fieldname": "item_group_name", "label": "Item Group Name",
         "fieldtype": "Data", "reqd": 1},
        {"fieldname": "parent_item_group", "label": "Parent Item Group",
         "fieldtype": "Link", "options": "Item Group"},
    ],
}


def _item_group_site():
    return FakeClient(
        records={"Item Group": [{"name": "Tooling-1",
                                 "item_group_name": "Tooling"}]},
        metas={"Item Group": ITEM_GROUP_META},
    )


# --------------------------------------------------------------- create_record
def test_create_record_finds_an_existing_record_through_its_natural_key():
    site = _item_group_site()

    result = create_record(site, "Item Group", {"item_group_name": "Tooling"})

    assert result == {"name": "Tooling-1", "created": False}
    assert ("insert", "Item Group") not in site.calls, "nothing to write: it exists"


def test_create_record_inserts_when_the_site_has_no_such_key():
    site = _item_group_site()

    first = create_record(site, "Item Group", {"item_group_name": "Raw Material"})

    assert first["created"] is True
    assert ("insert", "Item Group") in site.calls
    # ...and the row it just wrote is now findable, which is what makes a re-run
    # a no-op instead of a duplicate
    again = create_record(site, "Item Group", {"item_group_name": "Raw Material"})
    assert again["created"] is False


# ------------------------------------------------------- the existence queries
def test_existing_names_finds_what_the_site_has_by_the_key_field():
    site = FakeClient(records={"Item": [
        {"name": "ITEM-0001", "item_code": "MFG-1001"},
        {"name": "ITEM-0002", "item_code": "RAW-2001"},
    ]})

    found = existing_names(site, "Item", "item_code", ["MFG-1001", "SUB-3001"])

    assert found == {"MFG-1001"}


def test_existing_keys_reads_the_key_fields_of_every_record():
    """The spec path lists records and intersects in Python rather than filtering,
    and a composite spec joins two fields — a wrong join skips real rows or
    duplicates them, and nothing else in the suite reads this path."""
    site = FakeClient(records={"Address": [
        {"name": "ADDR-0001", "address_title": "Acme Steel - Billing",
         "address_type": "Billing"},
        {"name": "ADDR-0002", "address_title": "Acme Steel - Shipping",
         "address_type": "Shipping"},
    ]})

    assert existing_keys(site, "Address", ("address_title", "address_type")) == {
        "Acme Steel - Billing|Billing",
        "Acme Steel - Shipping|Shipping",
    }
    assert existing_keys(site, "Address", "address_title") == {
        "Acme Steel - Billing", "Acme Steel - Shipping"}
    assert existing_keys(site, "Employee", "employee_number") == set()


def test_dedup_payloads_skips_what_the_site_already_has():
    site = FakeClient(records={"Item": [
        {"name": "ITEM-0001", "item_code": "MFG-1001"}]})
    plan = MappingPlan(doctype="Item", id_field="item_code")
    payloads = [{"item_code": "MFG-1001", "__row": 2},
                {"item_code": "RAW-2001", "__row": 3},
                {"item_code": "RAW-2001", "__row": 4}]

    to_create, skipped = dedup_payloads(site, "Item", plan, payloads)

    assert [p["item_code"] for p in to_create] == ["RAW-2001"]
    # the two skips happen in different passes, so key on the row, not the order
    assert {p["item_code"]: p["__skip_reason"] for p in skipped} == {
        "MFG-1001": "already exists",
        "RAW-2001": "duplicate within source",
    }


def test_dedup_payloads_asks_about_the_documented_key_of_a_naming_series_doctype():
    """Employee is named `HR-EMP-#####`, so its existence query must ask about
    `employee_number`. Asking about `name` finds nothing, and the whole sheet is
    duplicated on every re-run."""
    site = FakeClient(records={"Employee": [
        {"name": "HR-EMP-0001", "employee_number": "EMP-001"}]})
    plan = MappingPlan(doctype="Employee", id_field="name")

    to_create, skipped = dedup_payloads(site, "Employee", plan,
                                        [{"employee_number": "EMP-001",
                                          "__row": 2}])

    assert to_create == [], "the row exists — it must not be created again"
    assert skipped[0]["__skip_reason"] == "already exists"


# ------------------------------------------------------------ the double itself
def test_the_double_refuses_a_filter_operator_it_does_not_model():
    """An unmodelled operator must fail loudly: silently matching everything is
    precisely the drift that let a poisoned filter pass the whole suite."""
    site = FakeClient(records={"Item": [{"name": "ITEM-0001"}]})

    with pytest.raises(AssertionError, match="does not model"):
        site.list("Item", filters=[["name", "regex", "ITEM"]])


# ------------------------------------------------- the reads the agent relies on
def test_get_record_returns_the_document_and_404s_when_it_is_gone():
    site = FakeClient(existing={("Item", "MFG-1001")},
                      docs={("Item", "MFG-1001"): {"name": "MFG-1001",
                                                   "item_group": "Products"}})

    assert get_record(site, "Item", "MFG-1001")["item_group"] == "Products"
    with pytest.raises(Exception, match="404"):
        get_record(site, "Item", "GONE-9999")


def test_list_records_forwards_the_query_and_projects_the_fields():
    """The tool the agent uses to check what a site holds: it must pass the
    filter through (not fetch everything) and project only what was asked for."""
    site = FakeClient(records={"Item Group": [
        {"name": "Tooling-1", "item_group_name": "Tooling", "is_group": 0},
        {"name": "Raw-1", "item_group_name": "Raw Material", "is_group": 0},
    ]})

    rows = list_records(site, "Item Group",
                        filters=[["item_group_name", "=", "Tooling"]],
                        fields=["name"], limit=5)

    assert rows == [{"name": "Tooling-1"}], "filtered *and* projected"
    assert site.calls == [("list", "Item Group")]


# -------------------------------------------------------- the dedup key label
def test_dedup_key_label_names_the_key_the_run_dedupes_on():
    """It is what the operator reads in the log when a row cannot be deduped, so
    it has to name the real key rather than a generic 'name'."""
    plan = MappingPlan(doctype="Item", id_field="item_code")
    assert dedup_key_label("Employee", plan, []) == "employee_number"

    plan_name = MappingPlan(doctype="Employee", id_field="name")
    payloads = [{"employee_number": "EMP-001"}]
    assert dedup_key_label("Employee", plan_name, payloads) == "employee_number"

    assert dedup_key_label("Item", plan, payloads) == "item_code"


def test_a_row_with_no_key_is_logged_as_failed_not_deduped_silently():
    """Without a key a row cannot be deduped at all: it is dropped *and* recorded,
    or the operator never learns that a row went missing."""
    class _Logger:
        def __init__(self):
            self.rows = []

        def row(self, row_number, key, status, docname=None, message=""):
            self.rows.append((row_number, key, status, message))

    site = FakeClient()
    logger = _Logger()
    plan = MappingPlan(doctype="Item", id_field="item_code")

    to_create, skipped = dedup_payloads(
        site, "Item", plan,
        [{"item_code": "", "__row": 7}, {"item_code": "RAW-2001", "__row": 8}],
        logger=logger)

    assert [p["item_code"] for p in to_create] == ["RAW-2001"]
    assert skipped == []
    [(row_no, key, status, message)] = logger.rows
    assert (row_no, key, status) == (7, "", "failed")
    assert "item_code" in message
