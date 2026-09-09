"""Parties import: a flat SME customer sheet with inline contact + address.

Real SME workbooks put contact and address fields in the CUSTOMER row. This
module splits one flat sheet into the three ERPNext doctypes — Customer,
Contact (email/phone children + Dynamic Link), Address (lines + Dynamic Link) —
and imports them in dependency order with idempotent keys:

  * Customer  -> dedup by customer_name
  * Contact   -> dedup by email
  * Address   -> dedup by address_title + address_type

Supported flat header (the "SMB format"):

  Customer Name, Customer Type, Group, Territory,
  Contact Name, Email, Phone,
  Address Type, Address Line 1, City, State, Postal Code, Country

A customer may appear on multiple rows (e.g. Billing + Shipping addresses); the
same email appearing twice yields ONE contact.
"""
from __future__ import annotations

from typing import Optional

from .client import ERPNextClient
from .logger import RunLogger
from .source import SourceTable

# flat column -> (target doctype, payload key).  "contact_name"/"email"/"phone"
# are synthetic keys handled specially during payload construction.
FLAT_MAP = {
    "Customer Name": ("customer", "customer_name"),
    "Customer Type": ("customer", "customer_type"),
    "Group": ("customer", "customer_group"),
    "Territory": ("customer", "territory"),
    "Contact Name": ("contact", "contact_name"),
    "Email": ("contact", "email"),
    "Phone": ("contact", "phone"),
    "Address Type": ("address", "address_type"),
    "Address Line 1": ("address", "address_line1"),
    "Address Line 2": ("address", "address_line2"),
    "City": ("address", "city"),
    "State": ("address", "state"),
    "Postal Code": ("address", "pincode"),
    "Country": ("address", "country"),
}


def _split_contact_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ""


def build_payloads(source: SourceTable) -> list[dict]:
    """Turn each source row into {customer, contact, address} payload dicts."""
    idx = {h: i for i, h in enumerate(source.headers)}
    payloads: list[dict] = []
    for row in source.rows:
        def cell(h):
            i = idx.get(h)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        customer = {
            k: cell(h)
            for h, (_dt, k) in FLAT_MAP.items()
            if FLAT_MAP[h][0] == "customer" and cell(h)
        }
        contact_name = cell("Contact Name")
        email = cell("Email")
        phone = cell("Phone")

        contact: dict = {}
        if contact_name:
            first, last = _split_contact_name(contact_name)
            if first:
                contact["first_name"] = first
            if last:
                contact["last_name"] = last
        if email:
            contact["email_ids"] = [{"email_id": email, "is_primary": 1}]
        if phone:
            contact["phone_nos"] = [{"phone": phone, "is_primary_phone": 1}]
        contact["links"] = [{"link_doctype": "Customer", "link_name": cell("Customer Name")}]

        address = {
            k: cell(h)
            for h, (_dt, k) in FLAT_MAP.items()
            if FLAT_MAP[h][0] == "address" and cell(h)
        }
        address_type = address.get("address_type") or "Billing"
        address["address_type"] = address_type
        address["address_title"] = f"{cell('Customer Name')} - {address_type}"
        address["links"] = [{"link_doctype": "Customer", "link_name": cell("Customer Name")}]

        payloads.append({"customer": customer, "contact": contact, "address": address})
    return payloads


def import_parties(
    client: ERPNextClient,
    source: SourceTable,
    defaults: Optional[dict] = None,
    apply: bool = False,
    logger: Optional[RunLogger] = None,
) -> dict:
    """Import the flat parties sheet, deduping each doctype independently."""
    defaults = defaults or {}

    cust_names = {str(r["name"]) for r in client.list("Customer", fields=["name"], limit=0)}
    contact_emails = {str(r["email_id"]).strip() for r in
                      client.list("Contact", fields=["email_id"], limit=0)
                      if r.get("email_id")}
    addr_keys = {
        f"{r.get('address_title')}|{r.get('address_type')}".strip()
        for r in client.list("Address", fields=["address_title", "address_type"], limit=0)
    }

    counts = {d: {"created": 0, "skipped": 0}
              for d in ("customer", "contact", "address")}

    for i, p in enumerate(build_payloads(source), start=2):
        # ---- customer ----
        cust = dict(p["customer"])
        for k, v in defaults.items():
            cust.setdefault(k, v)
        name = cust.get("customer_name", "").strip()
        if not name:
            if logger:
                logger.row(i, "", "failed", message="missing Customer Name")
            continue
        if name in cust_names:
            counts["customer"]["skipped"] += 1
            status = "skipped"
        else:
            if apply:
                client.insert("Customer", cust)
            counts["customer"]["created"] += 1
            cust_names.add(name)
            status = "created"
        if logger:
            logger.log(event="row", row=i, doctype="Customer", name=name, status=status)

        # ---- contact ----
        contact = p["contact"]
        email = (contact.get("email_ids") or [{}])[0].get("email_id", "")
        if email:
            if email in contact_emails:
                counts["contact"]["skipped"] += 1
                cstatus = "skipped"
            else:
                if apply:
                    client.insert("Contact", contact)
                counts["contact"]["created"] += 1
                contact_emails.add(email)
                cstatus = "created"
            if logger:
                logger.log(event="row", row=i, doctype="Contact", key=email, status=cstatus)

        # ---- address ----
        addr = p["address"]
        akey = f"{addr.get('address_title')}|{addr.get('address_type')}".strip()
        if akey in addr_keys:
            counts["address"]["skipped"] += 1
            astatus = "skipped"
        else:
            if apply:
                client.insert("Address", addr)
            counts["address"]["created"] += 1
            addr_keys.add(akey)
            astatus = "created"
        if logger:
            logger.log(event="row", row=i, doctype="Address",
                       key=addr.get("address_title"), status=astatus)

    return counts


def is_parties_sheet(source: SourceTable) -> bool:
    """Detect the flat SMB format from the header columns.

    True when the sheet has a customer-identity column AND at least one inline
    contact or address column (the signature of a flat party sheet).
    """
    hs = set(source.headers)
    return "Customer Name" in hs and ("Contact Name" in hs or "Address Line 1" in hs)


def run_parties_import(
    client: ERPNextClient,
    source: SourceTable,
    defaults: Optional[dict] = None,
    apply: bool = False,
    logger: Optional[RunLogger] = None,
) -> dict:
    counts = import_parties(client, source, defaults=defaults, apply=apply, logger=logger)
    print(f"Flat parties import ({'APPLY' if apply else 'dry run'}):")
    for doctype, c in counts.items():
        print(f"  {doctype:<10} created {c['created']} | skipped {c['skipped']}")
    return counts


