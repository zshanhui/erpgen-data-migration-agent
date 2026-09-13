"""P0: link-value validation must not invent conflicts.

Two regressions are guarded here, both of which produced an *unsatisfiable*
`link_value_conflict` that made the agent loop forever:

  * `Contact.links.link_name` is a **Dynamic Link**: its `options` is a sibling
    FIELD name ("link_doctype"), not a doctype. Treating it as a doctype made
    every value look missing — and told the agent to create records in a doctype
    that does not exist.
  * An unqueryable linked doctype must mean "unverifiable", never "nothing
    exists".

Pure: no network. `_Client` only implements `list`.
"""
from __future__ import annotations

import pytest

from conftest import make_field, make_meta, make_sheet
from erpgen.analysis import build_analysis
from erpgen.client import ERPNextError
from erpgen.conflicts import (UnverifiableLink, existing_values,
                              missing_link_values)
from erpgen.mapper import MappingEngine
from erpgen.metadata import FieldMeta

HEADERS = ["First Name", "Link Document Type (Links)", "Link Name (Links)"]
ROW = ["Alicia", "Customer", "Acme Steel Works"]


class _Client:
    """Only `list` is used by the link checks."""

    def __init__(self, values=None, fail_for=()):
        self.values = values or {}
        self.fail_for = set(fail_for)
        self.queries: list[str] = []

    def list(self, doctype, filters=None, fields=None, limit=0, order_by=None):
        self.queries.append(doctype)
        if doctype in self.fail_for or doctype not in self.values:
            raise ERPNextError(f"HTTP 404 GET /api/resource/{doctype}")
        return [{"name": n} for n in self.values[doctype]]


def _dynamic_link_meta():
    return make_meta("Dynamic Link", [
        make_field("link_doctype", "Link Document Type",
                   fieldtype="Link", options="DocType"),
        # the trap: options is a FIELD name
        make_field("link_name", "Link Name",
                   fieldtype="Dynamic Link", options="link_doctype"),
    ], istable=True)


def _analyse(client):
    parent = make_meta("Contact", [
        make_field("first_name", "First Name"),
        make_field("links", "Links", fieldtype="Table", options="Dynamic Link"),
    ])
    engine = MappingEngine(parent, {"Dynamic Link": _dynamic_link_meta()})
    source = make_sheet(HEADERS, [ROW])
    return build_analysis(client, source, engine.suggest(source), engine)


def _link_conflicts(analysis):
    return [c for c in analysis["conflicts"] if c["kind"] == "link_value_conflict"]


# --------------------------------------------------- the Dynamic Link bug
def test_dynamic_link_column_is_not_reported_as_missing():
    """The exact bug: 1 error conflict naming doctype 'link_doctype'."""
    client = _Client({"DocType": ["Customer"]})
    assert _link_conflicts(_analyse(client)) == []


def test_the_dynamic_link_is_never_queried_as_a_doctype():
    client = _Client({"DocType": ["Customer"]})
    _analyse(client)
    assert "link_doctype" not in client.queries, (
        "a Dynamic Link's options is a field name, never a doctype to query")


def test_real_link_columns_are_still_validated():
    """Guards against over-fixing: a genuine Link must still be checked."""
    client = _Client({"Country": ["China"]})   # queryable, but "Atlantis" absent
    parent = make_meta("Contact", [make_field("country", "Country",
                                              fieldtype="Link", options="Country")])
    engine = MappingEngine(parent, {})
    source = make_sheet(["Country"], [["Atlantis"]])
    analysis = build_analysis(client, source, engine.suggest(source), engine)
    got = _link_conflicts(analysis)
    assert len(got) == 1
    assert got[0]["doctype"] == "Country"
    assert got[0]["missing_values"] == ["Atlantis"]


def test_unqueryable_linked_doctype_is_unverifiable_not_all_missing():
    """'I could not check' must not be reported as 'nothing exists'."""
    client = _Client({"DocType": ["Customer"]}, fail_for={"Country"})
    parent = make_meta("Contact", [make_field("country", "Country",
                                              fieldtype="Link", options="Country")])
    engine = MappingEngine(parent, {})
    source = make_sheet(["Country"], [["United States"]])
    analysis = build_analysis(client, source, engine.suggest(source), engine)
    assert _link_conflicts(analysis) == []


# ------------------------------------------------------ metadata properties
def test_is_dynamic_link_is_distinct_from_is_link():
    dyn = FieldMeta(fieldname="link_name", label="Link Name",
                    fieldtype="Dynamic Link", options="link_doctype")
    plain = FieldMeta(fieldname="country", label="Country",
                      fieldtype="Link", options="Country")
    assert dyn.is_link and plain.is_link, "both are 'links' for traversal purposes"
    assert dyn.is_dynamic_link is True
    assert plain.is_dynamic_link is False


@pytest.mark.parametrize("fieldtype,options,expected", [
    ("Link", "Country", "Country"),
    ("Dynamic Link", "link_doctype", None),
    ("Link", None, None),
    ("Link", "", None),
    ("Data", "Country", None),
])
def test_links_to_doctype(fieldtype, options, expected):
    f = FieldMeta(fieldname="f", label="F", fieldtype=fieldtype, options=options)
    assert f.links_to_doctype == expected


# --------------------------------------------- unverifiable helpers (direct)
def test_existing_values_raises_when_the_doctype_is_unknown():
    with pytest.raises(UnverifiableLink):
        existing_values(_Client(), "link_doctype")


def test_existing_values_caches_a_failure_rather_than_requerying():
    client = _Client()
    cache: dict = {}
    for _ in range(2):
        with pytest.raises(UnverifiableLink):
            existing_values(client, "link_doctype", cache)
    assert client.queries == ["link_doctype"], "must not hammer an unknown doctype"


def test_existing_values_returns_names_when_it_can():
    assert existing_values(_Client({"Country": ["China", "Malaysia"]}), "Country") \
        == {"China", "Malaysia"}


def test_missing_link_values_returns_none_not_all_values():
    client = _Client(fail_for={"Country"})
    assert missing_link_values(client, ["China"], "Country") is None


def test_missing_link_values_reports_only_absent_ones():
    client = _Client({"Country": ["China"]})
    assert missing_link_values(client, ["China", "Atlantis"], "Country") == ["Atlantis"]


def test_missing_link_values_is_empty_when_all_exist():
    client = _Client({"Country": ["China"]})
    assert missing_link_values(client, ["China"], "Country") == []
