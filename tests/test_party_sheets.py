"""P0: flat party sheets — the Customer/Supplier flat-flow generalization.

Pure: no network, no live ERPNext site, no Docker (same contract as the rest of
this suite). Covers the code added for Supplier support:

  * the PARTY_SPECS registry and the per-party flat contract
  * detect_party_sheet — flat sheets vs ordinary relational sheets
  * parse_flat_target / load_flat_mappings — the contract-override plumbing
  * build_payloads — party routing, **type conversion**, mirror_columns,
    out-of-contract routing, and the "links are the importer's job" contract
  * _link_or_create — cross-party link-merge (Customer + Supplier), idempotency,
    dry-run prediction, and retryable failures
  * import_flat_parties — per-row failure semantics and orphan prevention
"""
from __future__ import annotations

import copy

import pytest

from conftest import make_sheet
from erpgen.client import ERPNextError
from erpgen.customers_full import (
    ADDRESS_COLUMNS,
    CONTACT_COLUMNS,
    PARTY_SPECS,
    _link_or_create,
    build_party_sheet_analysis,
    build_payloads,
    detect_party_sheet,
    flat_map_for,
    flow_for_party,
    import_flat_parties,
    is_party_sheet,
    load_flat_mappings,
    parse_flat_target,
    party_for_flow,
    run_flat_parties_import,
    spec_for,
    type_maps,
)

# ------------------------------------------------------------------ fixtures
CUSTOMER_HEADERS = [
    "Customer Name", "Customer Type", "Group", "Territory", "Contact Name",
    "Email", "Phone", "Address Type", "Address Line 1", "City", "State",
    "Postal Code", "Country",
]
SUPPLIER_HEADERS = [
    "Supplier Name", "Supplier Type", "Supplier Group", "Contact Name",
    "Email", "Phone", "Address Type", "Address Line 1", "City", "State",
    "Postal Code", "Country",
]

ACME_ROW = ["Acme Steel Works", "Company", "Commercial", "All Territories",
            "Alicia Henderson", "alicia@acmesteel.example", "+1 555 2100",
            "Billing", "100 Foundry Way", "Cleveland", "Ohio", "44101",
            "United States"]
# same company seen as a SUPPLIER (no Territory column) — the shared-party case
ACME_SUPPLIER_ROW = ["Acme Steel Works", "Company", "Raw Material",
                     "Alicia Henderson", "alicia@acmesteel.example", "+1 555 2100",
                     "Billing", "100 Foundry Way", "Cleveland", "Ohio", "44101",
                     "United States"]
SHENZHEN_ROW = ["Shenzhen Precision Castings Ltd", "Company", "Raw Material",
                "Wei Zhang", "wei.zhang@shenzhencast.example",
                "+86 755 5550 1234", "Billing", "Block 7 Bao'an Industrial Park",
                "Shenzhen", "Guangdong", "518101", "China"]


def _raw(name, fields, autoname=None, istable=False):
    return {"name": name, "autoname": autoname, "istable": int(istable),
            "is_submittable": 0, "fields": fields}


def _f(fieldname, label, fieldtype="Data", **kw):
    return {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, **kw}


#: Minimal live metadata for the four doctypes the flat party flow touches.
META = {
    "Customer": _raw("Customer", [
        _f("customer_name", "Customer Name"),
        _f("customer_type", "Customer Type"),
        _f("customer_group", "Customer Group", "Link", options="Customer Group"),
        _f("territory", "Territory", "Link", options="Territory"),
    ]),
    "Supplier": _raw("Supplier", [
        _f("supplier_name", "Supplier Name", reqd=1),
        _f("supplier_type", "Supplier Type", reqd=1),
        _f("supplier_group", "Supplier Group", "Link", options="Supplier Group"),
        _f("country", "Country", "Link", options="Country"),
        _f("default_currency", "Default Currency", "Link", options="Currency"),
        _f("is_transporter", "Is Transporter", "Check"),
        _f("lead_time_days", "Lead Time Days", "Int"),
        _f("payment_terms", "Payment Terms", "Link", options="Payment Terms Template"),
    ]),
    "Contact": _raw("Contact", [
        _f("first_name", "First Name"),
        _f("last_name", "Last Name"),
        _f("email_id", "Email Address"),
    ]),
    "Address": _raw("Address", [
        _f("address_title", "Address Title"),
        _f("address_type", "Address Type", "Select", reqd=1),
        _f("address_line1", "Address Line 1", reqd=1),
        _f("city", "City", reqd=1),
        _f("state", "State"),
        _f("pincode", "Pincode"),
        _f("country", "Country", "Link", options="Country", reqd=1),
    ]),
    # parent + child, for the describe_doctype child-requirement test
    "Payment Terms Template": _raw("Payment Terms Template", [
        _f("template_name", "Template Name"),
        _f("terms", "Payment Terms", "Table",
           options="Payment Terms Template Detail"),
    ], autoname="field:template_name"),
    "Payment Terms Template Detail": _raw("Payment Terms Template Detail", [
        _f("invoice_portion", "Invoice Portion (%)", "Float", reqd=1),
        _f("due_date_based_on", "Due Date Based On", "Select", reqd=1,
           options="Day(s) after invoice date\n"
                   "Day(s) after the end of the invoice month"),
        _f("credit_days", "Credit Days", "Int"),
    ], istable=True),
}

_NAMED_BY = {"Customer": "customer_name", "Supplier": "supplier_name"}


class FakeSite:
    """Duck-typed ERPNextClient: in-memory docs, no network."""

    def __init__(self, docs=None, fail_insert=()):
        self.docs = {dt: dict(d) for dt, d in (docs or {}).items()}
        self.fail_insert = set(fail_insert)
        self.inserted = []
        self.updated = []
        self._n = 0

    # reads
    def doctype_meta(self, dt):
        return META[dt]

    def list(self, doctype, filters=None, fields=None, limit=0, order_by=None):
        if doctype == "Custom Field":
            return []
        out = []
        for name, doc in self.docs.get(doctype, {}).items():
            row = {"name": name}
            for f in (fields or []):
                if f != "name":
                    row[f] = doc.get(f)
            out.append(row)
        return out

    def get(self, dt, name):
        if name not in self.docs.get(dt, {}):
            raise ERPNextError(f"HTTP 404 GET /api/resource/{dt}/{name}")
        return copy.deepcopy(self.docs[dt][name])

    # writes
    def insert(self, dt, doc):
        if dt in self.fail_insert:
            raise ERPNextError(f"MandatoryError: [{dt}]: mandatory fields missing")
        self._n += 1
        named = _NAMED_BY.get(dt)
        name = (doc.get(named) if named else None) or f"{dt}-{self._n}"
        rec = {**copy.deepcopy(doc), "name": name}
        if dt == "Contact":
            # Frappe computes Contact.email_id (read-only) from the primary
            # email_ids row — emulate it, or email-based dedup can't be tested.
            emails = doc.get("email_ids") or []
            if emails:
                rec["email_id"] = emails[0].get("email_id")
        self.docs.setdefault(dt, {})[name] = rec
        self.inserted.append((dt, copy.deepcopy(doc)))
        return copy.deepcopy(rec)

    def update(self, dt, name, doc):
        self.updated.append((dt, name, copy.deepcopy(doc)))
        self.docs[dt][name].update(copy.deepcopy(doc))
        return copy.deepcopy(self.docs[dt][name])


def _links(dt, name, links):
    return {dt: {name: {"name": name, "links": list(links)}}}


SUPPLIER_LINK = {"link_doctype": "Supplier", "link_name": "Acme Steel Works"}


# ------------------------------------------------------- registry / contract
def test_spec_for_known_parties():
    assert spec_for("Customer")["flow"] == "customers_full"
    assert spec_for("Customer")["key"] == "customer"
    assert spec_for("Customer")["name_field"] == "customer_name"
    assert spec_for("Supplier")["flow"] == "suppliers_full"
    assert spec_for("Supplier")["key"] == "supplier"
    assert spec_for("Supplier")["name_field"] == "supplier_name"


def test_spec_for_unknown_party_raises():
    with pytest.raises(ValueError) as e:
        spec_for("Item")
    assert "unknown party type" in str(e.value)


def test_flow_and_party_map_both_ways():
    assert flow_for_party("Supplier") == "suppliers_full"
    assert flow_for_party("Customer") == "customers_full"
    assert party_for_flow("suppliers_full") == "Supplier"
    assert party_for_flow("customers_full") == "Customer"
    assert party_for_flow("Item") is None
    assert party_for_flow("suppliers") is None


def test_both_parties_have_exactly_two_variants():
    assert set(PARTY_SPECS) == {"Customer", "Supplier"}


def test_flat_map_supplier_has_no_territory_and_own_group():
    m = flat_map_for("Supplier")
    assert m["Supplier Name"] == ("supplier", "supplier_name")
    assert m["Supplier Group"] == ("supplier", "supplier_group")
    assert m["Supplier Type"] == ("supplier", "supplier_type")
    assert "Territory" not in m
    assert "Customer Name" not in m


def test_flat_map_customer_has_territory_and_no_supplier_keys():
    m = flat_map_for("Customer")
    assert m["Customer Name"] == ("customer", "customer_name")
    assert m["Territory"] == ("customer", "territory")
    assert "Supplier Name" not in m
    assert "Supplier Group" not in m


def test_flat_map_shares_contact_and_address_columns():
    for party in ("Customer", "Supplier"):
        m = flat_map_for(party)
        for col in CONTACT_COLUMNS:
            assert m[col][0] == "contact"
        for col in ADDRESS_COLUMNS:
            assert m[col][0] == "address"


# ------------------------------------------------------------- sheet detection
def test_detect_customer_and_supplier_sheets():
    assert detect_party_sheet(make_sheet(CUSTOMER_HEADERS, [ACME_ROW])) == "Customer"
    assert detect_party_sheet(make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW])) == "Supplier"


def test_detect_ignores_relational_party_sheet():
    """A plain customers.csv (no inline contact/address) is NOT a flat sheet."""
    headers = ["Customer Name", "Customer Type", "Group", "Territory", "Tax ID"]
    assert detect_party_sheet(make_sheet(headers)) is None
    assert is_party_sheet(make_sheet(headers)) is False


def test_detect_requires_the_party_name_column():
    headers = ["Contact Name", "Email", "Address Line 1", "City"]
    assert detect_party_sheet(make_sheet(headers)) is None


def test_detect_accepts_address_only_sheets():
    headers = ["Supplier Name", "Address Line 1", "City", "Country"]
    assert detect_party_sheet(make_sheet(headers)) == "Supplier"


# ----------------------------------------------------------- flat overrides
@pytest.mark.parametrize("target,expected", [
    ("customer.tax_id", ("customer", "tax_id")),
    ("supplier.tax_id", ("supplier", "tax_id")),
    ("contact.mobile_no", ("contact", "mobile_no")),
    ("address.county", ("address", "county")),
    ("SUPPLIER.tax_id", ("supplier", "tax_id")),
])
def test_parse_flat_target_valid(target, expected):
    assert parse_flat_target(target) == expected


@pytest.mark.parametrize("target", [
    "", "tax_id", "customer.", ".tax_id", "item.code", "sales_order.customer",
])
def test_parse_flat_target_invalid(target):
    assert parse_flat_target(target) is None


def test_load_flat_mappings_scopes_to_the_flow(tmp_path, cli):
    from erpgen.overrides import save_overrides

    p = tmp_path / "ov.json"
    save_overrides(p, {
        "customers_full": {"mappings": {"Tax ID": "customer.tax_id",
                                        "Bogus": "nope.nope",
                                        "NoDot": "tax_id"}},
        "suppliers_full": {"mappings": {"Tax ID": "supplier.tax_id"}},
    })
    assert load_flat_mappings(p, "suppliers_full") == {"Tax ID": ("supplier", "tax_id")}
    assert load_flat_mappings(p, "customers_full") == {"Tax ID": ("customer", "tax_id")}
    assert load_flat_mappings(p, "unknown_flow") == {}


def test_load_flat_mappings_missing_file_is_empty(tmp_path):
    assert load_flat_mappings(tmp_path / "nope.json") == {}


# ------------------------------------------------------------ build_payloads
def test_build_payloads_routes_to_the_party_key():
    cust = build_payloads(make_sheet(CUSTOMER_HEADERS, [ACME_ROW]), "Customer")[0]
    assert set(cust) == {"customer", "contact", "address"}
    assert cust["customer"]["customer_name"] == "Acme Steel Works"
    assert cust["customer"]["customer_group"] == "Commercial"
    assert cust["customer"]["territory"] == "All Territories"

    sup = build_payloads(make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]), "Supplier")[0]
    assert set(sup) == {"supplier", "contact", "address"}
    assert sup["supplier"]["supplier_name"] == "Shenzhen Precision Castings Ltd"
    assert sup["supplier"]["supplier_group"] == "Raw Material"
    assert "territory" not in sup["supplier"]


def test_build_payloads_defaults_party_to_customer():
    assert "customer" in build_payloads(make_sheet(CUSTOMER_HEADERS, [ACME_ROW]))[0]


def test_build_payloads_splits_contact_name_and_children():
    p = build_payloads(make_sheet(CUSTOMER_HEADERS, [ACME_ROW]))[0]
    assert p["contact"]["first_name"] == "Alicia"
    assert p["contact"]["last_name"] == "Henderson"
    assert p["contact"]["email_ids"] == [
        {"email_id": "alicia@acmesteel.example", "is_primary": 1}]
    assert p["contact"]["phone_nos"] == [{"phone": "+1 555 2100", "is_primary_phone": 1}]


def test_build_payloads_address_title_uses_the_party_name_column():
    cust = build_payloads(make_sheet(CUSTOMER_HEADERS, [ACME_ROW]), "Customer")[0]
    sup = build_payloads(make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]), "Supplier")[0]
    assert cust["address"]["address_title"] == "Acme Steel Works - Billing"
    assert sup["address"]["address_title"] == "Shenzhen Precision Castings Ltd - Billing"


def test_build_payloads_defaults_address_type_to_billing():
    headers = [h for h in SUPPLIER_HEADERS if h != "Address Type"]
    row = [v for h, v in zip(SUPPLIER_HEADERS, SHENZHEN_ROW) if h != "Address Type"]
    p = build_payloads(make_sheet(headers, [row]), "Supplier")[0]
    assert p["address"]["address_type"] == "Billing"


def test_build_payloads_does_not_set_links():
    """Links belong to the importer, which knows the real document name."""
    p = build_payloads(make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]), "Supplier")[0]
    assert "links" not in p["contact"]
    assert "links" not in p["address"]


def test_build_payloads_mirrors_country_onto_the_supplier():
    """Supplier (unlike Customer) carries its own country field."""
    sup = build_payloads(make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]), "Supplier")[0]
    assert sup["supplier"]["country"] == "China"
    assert sup["address"]["country"] == "China"

    cust = build_payloads(make_sheet(CUSTOMER_HEADERS, [ACME_ROW]), "Customer")[0]
    assert "country" not in cust["customer"], "Customer has no country field"
    assert cust["address"]["country"] == "United States"


def test_build_payloads_routes_out_of_contract_columns():
    headers = SUPPLIER_HEADERS + ["Tax ID", "Lead Time Days"]
    row = SHENZHEN_ROW + ["91440300MA5EX1A2B3", "45"]
    flat = {"Tax ID": ("supplier", "tax_id"),
            "Lead Time Days": ("supplier", "lead_time_days")}
    p = build_payloads(make_sheet(headers, [row]), "Supplier", flat)[0]
    assert p["supplier"]["tax_id"] == "91440300MA5EX1A2B3"
    assert p["supplier"]["lead_time_days"] == "45"


def test_build_payloads_routes_out_of_contract_contact_and_address_columns():
    headers = SUPPLIER_HEADERS + ["Job Title", "County"]
    row = SHENZHEN_ROW + ["Procurement Lead", "Guangdong"]
    flat = {"Job Title": ("contact", "job_title"), "County": ("address", "county")}
    p = build_payloads(make_sheet(headers, [row]), "Supplier", flat)[0]
    assert p["contact"]["job_title"] == "Procurement Lead"
    assert p["address"]["county"] == "Guangdong"


def test_build_payloads_ignores_empty_cells():
    headers = SUPPLIER_HEADERS + ["Tax ID"]
    p = build_payloads(make_sheet(headers, [SHENZHEN_ROW + [""]]), "Supplier",
                       {"Tax ID": ("supplier", "tax_id")})[0]
    assert "tax_id" not in p["supplier"]


# ----------------------------------------------------- type conversion (bug)
def test_type_maps_reads_live_fieldtypes():
    tm = type_maps(FakeSite(), "Supplier")
    assert tm["supplier"]["is_transporter"] == "Check"
    assert tm["supplier"]["lead_time_days"] == "Int"
    assert tm["supplier"]["supplier_group"] == "Link"
    assert tm["contact"]["email_id"] == "Data"
    assert set(tm) == {"supplier", "contact", "address"}


def test_type_maps_uses_the_party_key_for_customers():
    assert set(type_maps(FakeSite(), "Customer")) == {"customer", "contact", "address"}


@pytest.mark.parametrize("raw,expected", [("Yes", 1), ("No", 0), ("true", 1),
                                          ("false", 0), ("1", 1), ("0", 0)])
def test_check_column_is_converted_not_stored_raw(raw, expected):
    """Regression: Frappe coerces the raw strings "Yes"/"true" to 0, so a flat
    sheet without conversion silently stored is_transporter = 0."""
    headers = SUPPLIER_HEADERS + ["Is Transporter"]
    row = SHENZHEN_ROW + [raw]
    tm = type_maps(FakeSite(), "Supplier")
    p = build_payloads(make_sheet(headers, [row]), "Supplier",
                       {"Is Transporter": ("supplier", "is_transporter")}, tm)[0]
    assert p["supplier"]["is_transporter"] == expected


def test_check_raw_string_would_be_wrong_without_type_map():
    """Documents why the type_map argument is required, not optional sugar."""
    headers = SUPPLIER_HEADERS + ["Is Transporter"]
    p = build_payloads(make_sheet(headers, [SHENZHEN_ROW + ["Yes"]]), "Supplier",
                       {"Is Transporter": ("supplier", "is_transporter")})[0]
    assert p["supplier"]["is_transporter"] == "Yes"  # raw string, not 1


def test_int_and_float_columns_are_converted():
    headers = SUPPLIER_HEADERS + ["Lead Time Days"]
    tm = {"supplier": {"lead_time_days": "Int"}}
    p = build_payloads(make_sheet(headers, [SHENZHEN_ROW + ["45"]]), "Supplier",
                       {"Lead Time Days": ("supplier", "lead_time_days")}, tm)[0]
    assert p["supplier"]["lead_time_days"] == 45
    assert isinstance(p["supplier"]["lead_time_days"], int)


def test_link_columns_stay_strings():
    tm = type_maps(FakeSite(), "Supplier")
    p = build_payloads(make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]), "Supplier",
                       None, tm)[0]
    assert p["supplier"]["default_currency"] if "default_currency" in p["supplier"] else True
    assert p["address"]["country"] == "China"


def test_unparseable_date_falls_back_to_the_raw_value():
    headers = SUPPLIER_HEADERS + ["Onboarded"]
    tm = {"supplier": {"onboarded": "Date"}}
    p = build_payloads(make_sheet(headers, [SHENZHEN_ROW + ["not-a-date"]]),
                       "Supplier", {"Onboarded": ("supplier", "onboarded")}, tm)[0]
    assert p["supplier"]["onboarded"] == "not-a-date"


def test_valid_date_is_normalized():
    headers = SUPPLIER_HEADERS + ["Onboarded"]
    tm = {"supplier": {"onboarded": "Date"}}
    p = build_payloads(make_sheet(headers, [SHENZHEN_ROW + ["2015/03/12"]]),
                       "Supplier", {"Onboarded": ("supplier", "onboarded")}, tm)[0]
    assert p["supplier"]["onboarded"] == "2015-03-12"


# ----------------------------------------------------------------- link-merge
def _call(client, link=None, apply=True, index=None, seen=None, key="acme@x.example"):
    """Default index says the contact already exists (the link-merge case)."""
    return _link_or_create(
        client, "Contact", {"first_name": "Alicia"}, key, link or SUPPLIER_LINK,
        index if index is not None else {key: "C1"},
        seen if seen is not None else {},
        apply, lambda m: None, 2,
    )


def test_link_merge_adds_a_second_party_link():
    """The point of the Supplier work: one Contact serving Customer AND Supplier."""
    c = FakeSite(_links("Contact", "C1",
                        [{"link_doctype": "Customer", "link_name": "Acme Steel Works"}]))
    status, msg = _call(c)
    assert (status, msg) == ("linked", "")
    assert c.docs["Contact"]["C1"]["links"] == [
        {"link_doctype": "Customer", "link_name": "Acme Steel Works"},
        SUPPLIER_LINK,
    ]


def test_link_merge_preserves_other_party_links():
    c = FakeSite(_links("Contact", "C1", [
        {"link_doctype": "Customer", "link_name": "Acme Steel Works"},
        {"link_doctype": "Supplier", "link_name": "Other Metals Ltd"},
    ]))
    _call(c)
    kinds = [(l["link_doctype"], l["link_name"]) for l in c.docs["Contact"]["C1"]["links"]]
    assert kinds == [("Customer", "Acme Steel Works"),
                     ("Supplier", "Other Metals Ltd"),
                     ("Supplier", "Acme Steel Works")]


def test_link_merge_is_idempotent_on_same_link():
    c = FakeSite(_links("Contact", "C1", [SUPPLIER_LINK]))
    status, _ = _call(c)
    assert status == "skipped"
    assert c.updated == [], "already-linked contact must not be written again"


def test_link_merge_dry_run_predicts_without_writing():
    c = FakeSite(_links("Contact", "C1",
                        [{"link_doctype": "Customer", "link_name": "Acme Steel Works"}]))
    status, _ = _call(c, apply=False)
    assert status == "linked"
    assert c.updated == []


def test_link_merge_dry_run_predicts_skip_when_already_linked():
    c = FakeSite(_links("Contact", "C1", [SUPPLIER_LINK]))
    assert _call(c, apply=False)[0] == "skipped"


def test_link_or_create_creates_then_links_earlier_in_run_contacts():
    """A contact created earlier in this dry run: 1st party links, repeat skips."""
    c = FakeSite()
    index, seen = {}, {}
    assert _call(c, apply=False, index=index, seen=seen)[0] == "created"
    assert index["acme@x.example"] is None, "dry run records a placeholder"
    assert _call(c, apply=False, index=index, seen=seen)[0] == "skipped"
    # a different party still needs its own link
    other = {"link_doctype": "Supplier", "link_name": "Other Metals Ltd"}
    assert _call(c, link=other, apply=False, index=index, seen=seen)[0] == "linked"


def test_link_or_create_sets_the_link_on_insert():
    c = FakeSite()
    status, _ = _call(c, key="new@x.example", index={})
    assert status == "created"
    assert c.inserted[0][1]["links"] == [SUPPLIER_LINK]


def test_link_or_create_failure_is_retryable():
    """A failed insert must NOT be recorded in the dedup index, so a re-run retries."""
    c = FakeSite(fail_insert={"Contact"})
    index: dict = {}
    status, msg = _call(c, key="new@x.example", index=index)
    assert status == "failed"
    assert "mandatory fields" in msg
    assert "new@x.example" not in index, "failed rows must stay retryable"


def test_link_or_create_missing_doc_fails_on_apply():
    c = FakeSite()  # no Contact/C1
    status, msg = _call(c, index={"acme@x.example": "C1"})
    assert status == "failed"
    assert "404" in msg


# ------------------------------------------------------------- import_flat_parties
def _accounted(counts, label):
    return counts[label]["created"], counts[label]["skipped"]


def test_import_creates_party_contact_and_address_with_links():
    site = FakeSite()
    src = make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW])
    counts = import_flat_parties(site, src, "Supplier", apply=True)
    assert _accounted(counts, "supplier") == (1, 0)
    assert _accounted(counts, "contact") == (1, 0)
    assert _accounted(counts, "address") == (1, 0)
    contact = site.inserted[1][1]
    assert contact["links"] == [
        {"link_doctype": "Supplier", "link_name": "Shenzhen Precision Castings Ltd"}]
    address = site.inserted[2][1]
    assert address["links"] == [
        {"link_doctype": "Supplier", "link_name": "Shenzhen Precision Castings Ltd"}]


def test_import_uses_the_real_document_name_for_links():
    """The insert result is authoritative, not the source's name column."""
    class RenamingSite(FakeSite):
        def insert(self, dt, doc):
            rec = super().insert(dt, doc)
            if dt == "Supplier":
                self.docs[dt]["SUP-0001"] = self.docs[dt].pop(rec["name"])
                self.docs[dt]["SUP-0001"]["name"] = "SUP-0001"
                return dict(self.docs[dt]["SUP-0001"])
            return rec

    site = RenamingSite()
    import_flat_parties(site, make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
                        "Supplier", apply=True)
    assert site.inserted[1][1]["links"][0]["link_name"] == "SUP-0001"


def test_import_is_idempotent_on_second_run():
    site = FakeSite()
    src = make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW])
    import_flat_parties(site, src, "Supplier", apply=True)
    counts = import_flat_parties(site, src, "Supplier", apply=True)
    assert _accounted(counts, "supplier") == (0, 1)
    assert _accounted(counts, "contact") == (0, 1)
    assert _accounted(counts, "address") == (0, 1)


def test_import_links_a_contact_already_linked_to_another_party():
    """Customer flow ran first; the Supplier flow must link, not duplicate."""
    site = FakeSite({
        "Customer": {"Acme Steel Works": {"name": "Acme Steel Works",
                                          "customer_name": "Acme Steel Works"}},
        "Contact": {"C1": {"name": "C1", "email_id": "alicia@acmesteel.example",
                           "links": [{"link_doctype": "Customer",
                                      "link_name": "Acme Steel Works"}]}},
    })
    counts = import_flat_parties(site, make_sheet(SUPPLIER_HEADERS, [ACME_SUPPLIER_ROW]),
                                 "Supplier", apply=True)
    assert _accounted(counts, "contact") == (0, 0)
    assert counts["contact"]["linked"] == 1
    assert _accounted(counts, "supplier") == (1, 0)
    kinds = [l["link_doctype"] for l in site.docs["Contact"]["C1"]["links"]]
    assert kinds == ["Customer", "Supplier"]


def test_import_skips_contact_and_address_when_the_party_insert_fails():
    """No orphan Contact/Address pointing at a party that does not exist."""
    site = FakeSite(fail_insert={"Supplier"})
    counts = import_flat_parties(site, make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
                                 "Supplier", apply=True)
    assert counts["supplier"]["failed"] == 1
    assert _accounted(counts, "contact") == (0, 0)
    assert _accounted(counts, "address") == (0, 0)
    assert site.inserted == []


def test_import_missing_party_name_is_a_failed_row():
    headers = [h for h in SUPPLIER_HEADERS if h != "Supplier Name"]
    row = [v for h, v in zip(SUPPLIER_HEADERS, SHENZHEN_ROW) if h != "Supplier Name"]
    site = FakeSite()
    counts = import_flat_parties(site, make_sheet(headers, [row]), "Supplier",
                                 apply=True)
    assert counts["supplier"]["failed"] == 1
    assert site.inserted == []


def test_import_dry_run_writes_nothing():
    site = FakeSite()
    counts = import_flat_parties(site, make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
                                 "Supplier", apply=False)
    assert _accounted(counts, "supplier") == (1, 0)
    assert site.inserted == []
    assert site.updated == []


def test_import_applies_defaults_to_the_party_only():
    site = FakeSite()
    src = make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW])
    import_flat_parties(site, src, "Supplier", defaults={"supplier_group": "Local"},
                        apply=True)
    party = site.inserted[0][1]
    assert party["supplier_group"] == "Raw Material", "existing value wins"
    assert "supplier_group" not in site.inserted[1][1]


def test_import_reports_a_missing_email_as_no_contact():
    headers = [h for h in SUPPLIER_HEADERS if h != "Email"]
    row = [v for h, v in zip(SUPPLIER_HEADERS, SHENZHEN_ROW) if h != "Email"]
    site = FakeSite()
    counts = import_flat_parties(site, make_sheet(headers, [row]), "Supplier",
                                 apply=True)
    assert _accounted(counts, "supplier") == (1, 0)
    assert _accounted(counts, "contact") == (0, 0)
    assert _accounted(counts, "address") == (1, 0)


def test_import_one_party_with_two_address_rows():
    headers = SUPPLIER_HEADERS + ["Address Line 2"]
    row_billing = SHENZHEN_ROW + ["Unit 4"]
    row_shipping = list(SHENZHEN_ROW)
    row_shipping[6] = "Shipping"           # Address Type
    row_shipping[7] = "Block 9 Port Road"  # Address Line 1
    row_shipping += ["Unit 1"]
    site = FakeSite()
    counts = import_flat_parties(site, make_sheet(headers, [row_billing, row_shipping]),
                                 "Supplier", apply=True)
    assert _accounted(counts, "supplier") == (1, 1)
    assert _accounted(counts, "address") == (2, 0)
    assert _accounted(counts, "contact") == (1, 1)


# ------------------------------------------------- link_value_conflict
def _site(payment_terms=(), groups=("Raw Material",), countries=("China",)):
    """FakeSite with the contract's Link targets seeded."""
    return FakeSite({
        "Payment Terms Template": {n: {"name": n} for n in payment_terms},
        "Supplier Group": {n: {"name": n} for n in groups},
        "Country": {n: {"name": n} for n in countries},
    })


def _analysis(site, headers, rows, flat=None, party="Supplier"):
    return build_party_sheet_analysis(site, make_sheet(headers, rows), party,
                                      flat_mappings=flat or {})


def _kinds(analysis, kind):
    return [c for c in analysis["conflicts"] if c["kind"] == kind]


PT_HEADERS = SUPPLIER_HEADERS + ["Payment Terms"]
PT_ROW = SHENZHEN_ROW + ["Net 30"]
PT_FLAT = {"Payment Terms": ("supplier", "payment_terms")}


def test_a_fully_valid_sheet_has_no_conflicts():
    a = _analysis(_site(), SUPPLIER_HEADERS, [SHENZHEN_ROW])
    assert _kinds(a, "link_value_conflict") == []
    assert a["conflicts"] == []


# ------------------------------------------------- out-of-contract columns
# `_classify_extra_column` decides what the playbook's extend_contract /
# create_custom_field advice is based on. Nothing exercised it: the fixtures
# never carried a column outside the contract, so a wrong target or an
# unactionable suggested_action would have reached the agent unnoticed.
def _with_extras(*extra):
    return SUPPLIER_HEADERS + list(extra), [SHENZHEN_ROW + ["x"] * len(extra)]


def test_an_extra_column_that_matches_a_known_field_extends_the_contract():
    headers, rows = _with_extras("Zip")
    a = _analysis(_site(), headers, rows)

    [entry] = a["extra_columns"]
    assert entry["resolution"] == "extend_contract"
    assert entry["best_match"] == {"doctype": "Address", "target": "pincode",
                                   "label": "Pincode", "score": 0.78,
                                   "method": "synonym"}

    [conflict] = _kinds(a, "unmapped_column")
    assert conflict["severity"] == "error", "an unmapped column is blocking"
    assert conflict["target"] == "pincode" and conflict["doctype"] == "Address"
    assert "set_mapping('suppliers_full', 'Zip', 'address.pincode')" \
        in conflict["suggested_action"]
    assert a["suggested_custom_fields"] == [], \
        "the field exists — create_custom_field would be the wrong advice"


def test_an_extra_column_with_no_match_asks_for_a_custom_field_first():
    headers, rows = _with_extras("Vendor Rating")
    a = _analysis(_site(), headers, rows)

    [entry] = a["extra_columns"]
    assert entry["resolution"] == "create_custom_field"
    assert entry["best_match"] is None
    assert entry["suggested_doctype"] == "Supplier"

    [conflict] = _kinds(a, "unmapped_column")
    assert conflict["suggested_action"].startswith(
        "create_field('Supplier', 'Vendor Rating', 'Data')")

    [suggested] = a["suggested_custom_fields"]
    assert suggested["fieldname"] == "vendor_rating"
    assert suggested["label"] == "Vendor Rating"
    assert "createfield Supplier --label 'Vendor Rating'" in suggested["create_command"]


def test_the_profile_of_an_extra_column_is_reported_for_review():
    headers, rows = _with_extras("Vendor Rating")
    a = _analysis(_site(), headers, rows)

    [entry] = a["extra_columns"]
    assert entry["non_empty"] == 1.0
    assert entry["sample"] == ["x"]
    assert entry["inferred_type"] == "text"


def test_both_classifications_reach_the_analysis_in_column_order():
    headers, rows = _with_extras("Zip", "Vendor Rating")
    a = _analysis(_site(), headers, rows)

    assert [(e["header"], e["resolution"]) for e in a["extra_columns"]] == [
        ("Zip", "extend_contract"),
        ("Vendor Rating", "create_custom_field"),
    ]
    assert [c["source"] for c in _kinds(a, "unmapped_column")] == \
        ["Zip", "Vendor Rating"]
    assert [s["fieldname"] for s in a["suggested_custom_fields"]] == ["vendor_rating"]


# ------------------------------------------------- the flat import orchestrator
def test_the_flat_import_reports_what_it_wrote(capsys):
    """The printed summary is the operator's (and the shell scripts') only view of
    a flat import: one line per doctype the row fed, with the counts it returns."""
    counts = run_flat_parties_import(
        FakeSite(), make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
        party="Supplier", apply=True)

    assert counts == {
        "supplier": {"created": 1, "skipped": 0, "failed": 0},
        "contact": {"created": 1, "skipped": 0, "linked": 0, "failed": 0},
        "address": {"created": 1, "skipped": 0, "linked": 0, "failed": 0},
    }
    out = capsys.readouterr().out
    assert "suppliers_full import (APPLY):" in out
    assert "supplier   created 1 | skipped 0" in out
    assert "contact    created 1 | skipped 0" in out
    assert "address    created 1 | skipped 0" in out


def test_the_flat_import_says_dry_run_when_it_is_not_applying(capsys):
    counts = run_flat_parties_import(
        FakeSite(), make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
        party="Supplier", apply=False)

    assert counts["supplier"]["created"] == 1, "a dry run still predicts the rows"
    assert "suppliers_full import (dry run):" in capsys.readouterr().out


def test_link_conflict_for_a_mapped_but_missing_link_value():
    a = _analysis(_site(), PT_HEADERS, [PT_ROW], PT_FLAT)
    got = _kinds(a, "link_value_conflict")
    assert len(got) == 1
    c = got[0]
    assert c["doctype"] == "Payment Terms Template"
    assert c["missing_values"] == ["Net 30"]
    assert c["targets"] == ["supplier.payment_terms"]
    assert c["severity"] == "error"
    assert "create_record" in c["suggested_action"]


def test_link_conflict_reports_only_the_missing_values():
    a = _analysis(_site(payment_terms=["Net 30"]), PT_HEADERS,
                  [PT_ROW, SHENZHEN_ROW + ["Net 45"]], PT_FLAT)
    c = _kinds(a, "link_value_conflict")[0]
    assert c["missing_values"] == ["Net 45"], "existing values must not be reported"


# ------------------------------------------------- link_group_node (flat path)
def _customer_site(groups):
    """Customer Group names, optionally flagged as group nodes. Territory and
    Country are seeded too, so only the group is under test."""
    return FakeSite({
        "Customer Group": groups,
        "Territory": {"All Territories": {"name": "All Territories"}},
        "Country": {"United States": {"name": "United States"}},
    })


def test_a_customer_group_node_is_reported_not_a_missing_record():
    """ERPNext throws 'Cannot select a Group type Customer Group' (HTTP 417), so
    every row would fail; the analysis must say so before the import runs."""
    a = _analysis(_customer_site({"All Customer Groups": {"name": "All Customer Groups",
                                                          "is_group": 1}}),
                  CUSTOMER_HEADERS,
                  [["Acme Steel Works", "Company", "All Customer Groups",
                    "All Territories"] + ACME_ROW[4:]],
                  party="Customer")

    assert _kinds(a, "link_value_conflict") == [], "the node exists — not a missing value"
    got = _kinds(a, "link_group_node")
    assert len(got) == 1
    c = got[0]
    assert c["doctype"] == "Customer Group"
    assert c["group_values"] == ["All Customer Groups"]
    assert c["targets"] == ["customer.customer_group"]
    assert c["severity"] == "error"
    assert "set_value" in c["suggested_action"]
    assert "value_map" not in c["suggested_action"], (
        "there is no value_map operation — the suggestion has to name a verb the "
        "agent can actually call")
    assert "leaf" in c["suggested_action"]
    assert "missing" not in c["suggested_action"], (
        "the value exists — 'create the missing record' is link_value_conflict's "
        "fix, and sending the agent after it would loop forever")


def test_a_leaf_customer_group_is_not_reported():
    a = _analysis(_customer_site({"Commercial": {"name": "Commercial", "is_group": 0}}),
                  CUSTOMER_HEADERS, [ACME_ROW], party="Customer")
    assert _kinds(a, "link_group_node") == []
    assert a["conflicts"] == []


def test_only_the_group_nodes_are_listed():
    a = _analysis(_customer_site({
        "All Customer Groups": {"name": "All Customer Groups", "is_group": 1},
        "Commercial": {"name": "Commercial", "is_group": 0},
    }), CUSTOMER_HEADERS,
        [ACME_ROW,
         ["Acme Two", "Company", "All Customer Groups", "All Territories"]
         + ACME_ROW[4:]], party="Customer")
    c = _kinds(a, "link_group_node")[0]
    assert c["group_values"] == ["All Customer Groups"]


def test_a_supplier_group_node_is_not_reported():
    """ERPNext only validates Customer.customer_group — Supplier has no such
    check, so flagging it would be a conflict ERPNext does not have."""
    site = FakeSite({"Supplier Group": {"Raw Material": {"name": "Raw Material",
                                                         "is_group": 1}},
                     "Country": {"China": {"name": "China"}}})
    a = _analysis(site, SUPPLIER_HEADERS, [SHENZHEN_ROW])
    assert _kinds(a, "link_group_node") == []


def test_contract_link_columns_are_validated():
    row = list(SHENZHEN_ROW)
    row[2] = "Nonexistent Group"
    c = _kinds(_analysis(_site(), SUPPLIER_HEADERS, [row]),
               "link_value_conflict")[0]
    assert c["doctype"] == "Supplier Group"
    assert c["missing_values"] == ["Nonexistent Group"]


def test_mirrored_and_address_targets_share_one_conflict():
    row = list(SHENZHEN_ROW)
    row[11] = "Atlantis"
    got = _kinds(_analysis(_site(), SUPPLIER_HEADERS, [row]), "link_value_conflict")
    assert len(got) == 1, "one column must not report twice for the same linked doctype"
    assert got[0]["targets"] == ["address.country", "supplier.country"]
    assert got[0]["missing_values"] == ["Atlantis"]


def test_data_columns_are_not_link_checked():
    a = _analysis(_site(), SUPPLIER_HEADERS + ["Tax ID"],
                  [SHENZHEN_ROW + ["91440300MA5EX1A2B3"]],
                  {"Tax ID": ("supplier", "tax_id")})
    assert _kinds(a, "link_value_conflict") == []


def test_synthetic_contact_columns_are_not_link_checked():
    a = _analysis(_site(), SUPPLIER_HEADERS, [SHENZHEN_ROW])
    assert _kinds(a, "link_value_conflict") == []


def test_unresolvable_field_is_not_link_checked():
    """A column mapped to a field that does not exist yet cannot be validated."""
    a = _analysis(_site(), SUPPLIER_HEADERS + ["Not A Field"],
                  [SHENZHEN_ROW + ["x"]],
                  {"Not A Field": ("supplier", "not_a_field")})
    assert _kinds(a, "link_value_conflict") == []


def test_resolved_column_is_no_longer_an_unmapped_conflict():
    a = _analysis(_site(payment_terms=["Net 30"]), PT_HEADERS, [PT_ROW], PT_FLAT)
    assert "Payment Terms" not in [c["source"] for c in _kinds(a, "unmapped_column")]


def test_values_from_rows_that_cannot_import_are_ignored():
    """A junk value in a row with no party name is skipped at import — it must not
    become a phantom conflict the agent would "fix" with junk master data.

    The blank party name itself *is* reported (phase 3): that row cannot import and
    saying so is the point. What must not appear is a link conflict built from a
    dropped row's junk values.
    """
    nameless = ["", "Company", "Raw Material", "Edge Contact", "edge@x.example", "",
                "Billing", "1 Edge St", "Edgeville", "", "00000", "EdgeCountry"]
    a = _analysis(_site(), SUPPLIER_HEADERS, [SHENZHEN_ROW, nameless])
    assert _kinds(a, "link_value_conflict") == []
    assert [c["kind"] for c in a["conflicts"]] == ["missing_value"]
    (blank_key,) = _kinds(a, "missing_value")
    assert blank_key["source"] == "Supplier Name"
    assert blank_key["roles"] == ["key", "required"]    # the row is dropped at import
    assert blank_key["rows"] == [3]


def test_link_conflict_clears_once_the_records_exist():
    """Mirrors the agent loop: create the records, re-map, conflict is gone."""
    assert _kinds(_analysis(_site(), PT_HEADERS, [PT_ROW], PT_FLAT),
                  "link_value_conflict")
    after = _analysis(_site(payment_terms=["Net 30"]), PT_HEADERS, [PT_ROW], PT_FLAT)
    assert _kinds(after, "link_value_conflict") == []


# ------------------------------------------------- describe_doctype
def test_describe_doctype_exposes_child_required_fields():
    """Without these an agent cannot build a valid child row."""
    from erpgen.tools import describe_doctype

    d = describe_doctype(FakeSite(), "Payment Terms Template")
    table = d["fields"]["tables"][0]
    assert table["child_doctype"] == "Payment Terms Template Detail"
    req = {r["fieldname"]: r for r in table["required"]}
    assert set(req) == {"invoice_portion", "due_date_based_on"}
    assert req["invoice_portion"]["fieldtype"] == "Float"
    assert req["due_date_based_on"]["options"] == [
        "Day(s) after invoice date",
        "Day(s) after the end of the invoice month",
    ]


def test_describe_doctype_without_child_tables():
    from erpgen.tools import describe_doctype

    assert describe_doctype(FakeSite(), "Supplier")["fields"]["tables"] == []


# ------------------------------------------------- journaling the flat import
class _Journal:
    """Records what the flat import asks to be journaled."""

    def __init__(self):
        self.created: list = []
        self.links: list = []

    def record_created(self, doctype, name):
        self.created.append((doctype, name))

    def link_added(self, doctype, name, link_doctype, link_name):
        self.links.append((doctype, name, link_doctype, link_name))


def test_flat_import_journals_every_created_record():
    site, journal = FakeSite(), _Journal()
    import_flat_parties(site, make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
                        "Supplier", apply=True, journal=journal)
    assert [dt for dt, _ in journal.created] == ["Supplier", "Contact", "Address"]
    assert journal.links == []


def test_flat_import_journals_the_link_merge_but_not_the_creates_it_replaces():
    """Only the added link is an effect; the existing contact is untouched."""
    site = FakeSite({
        "Customer": {"Acme Steel Works": {"name": "Acme Steel Works",
                                          "customer_name": "Acme Steel Works"}},
        "Contact": {"C1": {"name": "C1", "email_id": "alicia@acmesteel.example",
                           "links": [{"link_doctype": "Customer",
                                      "link_name": "Acme Steel Works"}]}},
        "Address": {"A1": {"name": "A1",
                           "address_title": "Acme Steel Works - Billing",
                           "address_type": "Billing"}},
    })
    journal = _Journal()
    import_flat_parties(site, make_sheet(SUPPLIER_HEADERS, [ACME_SUPPLIER_ROW]),
                        "Supplier", apply=True, journal=journal)
    assert ("Contact", "C1", "Supplier", "Acme Steel Works") in journal.links
    assert ("Address", "A1", "Supplier", "Acme Steel Works") in journal.links
    assert [dt for dt, _ in journal.created] == ["Supplier"], \
        "the reused contact/address must not be journaled as created"


def test_flat_import_journals_nothing_on_a_dry_run():
    journal = _Journal()
    import_flat_parties(FakeSite(), make_sheet(SUPPLIER_HEADERS, [SHENZHEN_ROW]),
                        "Supplier", apply=False, journal=journal)
    assert journal.created == [] and journal.links == []
