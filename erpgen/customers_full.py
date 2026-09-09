"""customers_full import: a flat SME customer sheet with inline contact + address.

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
same email appearing twice yields ONE contact. A contact shared by multiple
customers is created once and gets a Dynamic Link row per customer (idempotent
link-merge); addresses are one-per-customer (no physical-address dedup).
"""
from __future__ import annotations

import sys
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


def _ensure_contact_link(client: ERPNextClient, name: str, customer: str,
                         apply: bool) -> tuple[str, str]:
    """Append a Customer link to an existing Contact if missing.

    Returns (status, message):
      ("skipped", "") — already linked (no-op)
      ("linked", "")  — link added (apply) or would be added (dry-run)
      ("failed", msg) — the update errored on apply
    """
    try:
        existing = client.get("Contact", name)
    except Exception as e:
        return ("failed" if apply else "skipped"), (str(e) if apply else "")
    links = list(existing.get("links") or [])
    if any(str(r.get("link_doctype")) == "Customer"
           and str(r.get("link_name")) == customer for r in links):
        return "skipped", ""
    links.append({"link_doctype": "Customer", "link_name": customer})
    if apply:
        try:
            client.update("Contact", name, {"links": links})
        except Exception as e:
            return "failed", str(e)
    return "linked", ""


def import_customers_full(
    client: ERPNextClient,
    source: SourceTable,
    defaults: Optional[dict] = None,
    apply: bool = False,
    logger: Optional[RunLogger] = None,
) -> dict:
    """Import the flat customers_full sheet, deduping each doctype independently.

    Contacts dedup by email; when a contact is already present (on the site or
    earlier in this run) it is linked to each additional customer via a Dynamic
    Link row instead of being re-created.

    Per-row insert failures are logged as `failed` (with a stderr warning) and
    skipped, never aborting the run. A failed record is NOT added to its dedup
    set, so fixing the source and re-running will retry it — and re-link the
    address/contact to its customer.
    """
    defaults = defaults or {}

    cust_names = {str(r["name"]) for r in client.list("Customer", fields=["name"], limit=0)}
    # email -> contact name (None for contacts created within this dry run)
    contact_by_email: dict[str, Optional[str]] = {
        str(r["email_id"]).strip(): str(r["name"])
        for r in client.list("Contact", fields=["name", "email_id"], limit=0)
        if r.get("email_id")
    }
    # customers already attached per email (dry-run prediction for in-run contacts)
    contact_customers: dict[str, set] = {}
    addr_keys = {
        f"{r.get('address_title')}|{r.get('address_type')}".strip()
        for r in client.list("Address", fields=["address_title", "address_type"], limit=0)
    }

    counts = {
        "customer": {"created": 0, "skipped": 0, "failed": 0},
        "contact": {"created": 0, "skipped": 0, "linked": 0, "failed": 0},
        "address": {"created": 0, "skipped": 0, "failed": 0},
    }

    def warn(msg: str) -> None:
        print(f"WARNING: {msg}", file=sys.stderr)

    for i, p in enumerate(build_payloads(source), start=2):
        # ---- customer ----
        cust = dict(p["customer"])
        for k, v in defaults.items():
            cust.setdefault(k, v)
        name = cust.get("customer_name", "").strip()
        cstatus = ""
        cmessage = ""
        if not name:
            counts["customer"]["failed"] += 1
            cstatus = "failed"
            cmessage = "missing Customer Name"
            warn(f"row {i}: missing Customer Name (skipped)")
            if logger:
                logger.log(event="row", row=i, doctype="Customer", name="",
                           status=cstatus, message=cmessage)
            continue
        if name in cust_names:
            counts["customer"]["skipped"] += 1
            cstatus = "skipped"
        else:
            try:
                if apply:
                    client.insert("Customer", cust)
                counts["customer"]["created"] += 1
                cust_names.add(name)
                cstatus = "created"
            except Exception as e:
                counts["customer"]["failed"] += 1
                cstatus = "failed"
                cmessage = str(e)
                warn(f"row {i}: Customer '{name}' failed: {e}")
        if logger:
            logger.log(event="row", row=i, doctype="Customer", name=name,
                       status=cstatus, message=cmessage)
        if cstatus == "failed":
            # contact/address link to a customer that does not exist — skip them
            continue

        # ---- contact ----
        contact = p["contact"]
        email = (contact.get("email_ids") or [{}])[0].get("email_id", "")
        if email:
            customer = (contact.get("links") or [{}])[0].get("link_name", "")
            if email in contact_by_email:
                cname = contact_by_email[email]
                if cname is None:
                    # created earlier in this run (dry-run placeholder)
                    seen = contact_customers.setdefault(email, set())
                    cstatus = "skipped" if customer in seen else "linked"
                    seen.add(customer)
                    cmessage = ""
                else:
                    cstatus, cmessage = _ensure_contact_link(client, cname, customer, apply)
                counts["contact"][cstatus] += 1
            else:
                try:
                    if apply:
                        created = client.insert("Contact", contact)
                        contact_by_email[email] = created.get("name")
                    else:
                        contact_by_email[email] = None
                    contact_customers.setdefault(email, set()).add(customer)
                    counts["contact"]["created"] += 1
                    cstatus = "created"
                    cmessage = ""
                except Exception as e:
                    counts["contact"]["failed"] += 1
                    cstatus = "failed"
                    cmessage = str(e)
                    warn(f"row {i}: Contact '{email}' failed: {e}")
            if logger:
                logger.log(event="row", row=i, doctype="Contact", key=email,
                           status=cstatus, customer=customer, message=cmessage)

        # ---- address ----
        addr = p["address"]
        akey = f"{addr.get('address_title')}|{addr.get('address_type')}".strip()
        if akey in addr_keys:
            counts["address"]["skipped"] += 1
            astatus = "skipped"
            amessage = ""
        else:
            try:
                if apply:
                    client.insert("Address", addr)
                counts["address"]["created"] += 1
                addr_keys.add(akey)
                astatus = "created"
                amessage = ""
            except Exception as e:
                counts["address"]["failed"] += 1
                astatus = "failed"
                amessage = str(e)
                warn(f"row {i}: Address '{addr.get('address_title')}' failed: {e}")
        if logger:
            logger.log(event="row", row=i, doctype="Address",
                       key=addr.get("address_title"), status=astatus, message=amessage)

    return counts


def is_customers_full_sheet(source: SourceTable) -> bool:
    """Detect the flat SMB format from the header columns.

    True when the sheet has a customer-identity column AND at least one inline
    contact or address column (the signature of a flat customers_full sheet).
    """
    hs = set(source.headers)
    return "Customer Name" in hs and ("Contact Name" in hs or "Address Line 1" in hs)


def run_customers_full_import(
    client: ERPNextClient,
    source: SourceTable,
    defaults: Optional[dict] = None,
    apply: bool = False,
    logger: Optional[RunLogger] = None,
) -> dict:
    counts = import_customers_full(client, source, defaults=defaults, apply=apply, logger=logger)
    print(f"customers_full import ({'APPLY' if apply else 'dry run'}):")
    for doctype, c in counts.items():
        extra = ""
        if c.get("linked"):
            extra += f" | linked {c['linked']}"
        if c.get("failed"):
            extra += f" | failed {c['failed']}"
        print(f"  {doctype:<10} created {c['created']} | skipped {c['skipped']}{extra}")
    return counts


