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

import re
import sys
from datetime import datetime, timezone
from typing import Optional

from .client import ERPNextClient
from .logger import RunLogger
from .mapper import MappingEngine
from .metadata import fetch_with_children
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


_FLAT_DOCTYPES = {"customer", "contact", "address"}
_DT_CANONICAL = {"customer": "Customer", "contact": "Contact", "address": "Address"}


def _split_contact_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ""


def parse_flat_target(target: str) -> Optional[tuple[str, str]]:
    """Parse a flat mapping target '<doctype>.<fieldname>' -> (doctype, fieldname).

    Doctype is normalized to the flat payload key (customer/contact/address).
    Returns None when malformed or the doctype is not one of the three.
    """
    if not target or "." not in target:
        return None
    dt, field = target.split(".", 1)
    dt = dt.strip().lower()
    field = field.strip()
    if dt not in _FLAT_DOCTYPES or not field:
        return None
    return dt, field


def load_flat_mappings(path) -> dict[str, tuple[str, str]]:
    """Read the customers_full contract extensions from the overrides file.

    Returns {header: (doctype_key, fieldname)} for every `customers_full`
    mapping whose target parses as '<doctype>.<fieldname>'.
    """
    from .overrides import load_overrides

    raw = (load_overrides(path).get("customers_full") or {}).get("mappings") or {}
    out: dict[str, tuple[str, str]] = {}
    for header, target in raw.items():
        parsed = parse_flat_target(target)
        if parsed:
            out[header] = parsed
    return out


def build_payloads(
    source: SourceTable,
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
) -> list[dict]:
    """Turn each source row into {customer, contact, address} payload dicts.

    `flat_mappings` (header -> (doctype_key, fieldname)) carries resolved
    out-of-contract columns; their values are routed into the matching doctype.
    """
    extra = flat_mappings or {}
    idx = {h: i for i, h in enumerate(source.headers)}
    payloads: list[dict] = []
    for row in source.rows:
        def cell(h):
            i = idx.get(h)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        customer = {
            k: cell(h)
            for h, (dt, k) in FLAT_MAP.items()
            if dt == "customer" and cell(h)
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
            for h, (dt, k) in FLAT_MAP.items()
            if dt == "address" and cell(h)
        }
        address_type = address.get("address_type") or "Billing"
        address["address_type"] = address_type
        address["address_title"] = f"{cell('Customer Name')} - {address_type}"
        address["links"] = [{"link_doctype": "Customer", "link_name": cell("Customer Name")}]

        # resolved out-of-contract columns -> route into the right doctype payload
        for header, (dt, field) in extra.items():
            val = cell(header)
            if not val:
                continue
            if dt == "customer":
                customer[field] = val
            elif dt == "contact":
                contact[field] = val
            elif dt == "address":
                address[field] = val

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
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
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

    for i, p in enumerate(build_payloads(source, flat_mappings), start=2):
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
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    counts = import_customers_full(
        client, source, defaults=defaults, apply=apply, logger=logger,
        flat_mappings=flat_mappings,
    )
    print(f"customers_full import ({'APPLY' if apply else 'dry run'}):")
    for doctype, c in counts.items():
        extra = ""
        if c.get("linked"):
            extra += f" | linked {c['linked']}"
        if c.get("failed"):
            extra += f" | failed {c['failed']}"
        print(f"  {doctype:<10} created {c['created']} | skipped {c['skipped']}{extra}")
    return counts


# ------------------------------------------------------------- LLM analysis
def _snake(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip()).strip("_").lower()
    return re.sub(r"_+", "_", s)


def _fieldtype_for(profile) -> str:
    if profile is None:
        return "Data"
    return {
        "int": "Int",
        "float": "Float",
        "date": "Date",
        "bool": "Check",
    }.get(profile.inferred_type, "Data")


def build_customers_full_analysis(
    client: ERPNextClient,
    source: SourceTable,
    *,
    base_url: str = "",
    source_path: str = "",
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    """Build the flat customers_full mapping analysis for an LLM agent.

    Columns in the fixed contract (FLAT_MAP) are deterministic; every column
    outside that contract is surfaced as an `unmapped_column` conflict with one
    of two resolutions: map it to an existing field (extend_contract) or create
    a custom field (create_custom_field, with a ready `create_command`).
    Already-resolved flat overrides are treated as in-contract and NOT flagged.
    """
    engines: dict[str, MappingEngine] = {}
    for dt in ("Customer", "Contact", "Address"):
        parent, children = fetch_with_children(client, dt)
        engines[dt] = MappingEngine(parent, children)

    flat = flat_mappings or {}
    known = set(FLAT_MAP.keys()) | set(flat.keys())
    known_mappings = [
        {"header": h, "doctype": dt, "target": target, "method": "flat_contract"}
        for h, (dt, target) in FLAT_MAP.items()
        if h in source.headers
    ]
    known_mappings += [
        {"header": h, "doctype": dt, "target": field, "method": "flat_override"}
        for h, (dt, field) in flat.items()
        if h in source.headers
    ]

    conflicts: list[dict] = []
    suggested: list[dict] = []
    extra_columns: list[dict] = []

    for header in source.headers:
        if header in known:
            continue
        idx = source.column_index(header)
        profile = source.profiles[idx] if idx is not None and idx < len(source.profiles) else None

        # best existing-field match across the three doctypes (skip the
        # synthetic "name"/"ID" dedup target — it would token-match e.g. "Tax ID")
        best_doctype: Optional[str] = None
        best_target = None
        best_score = 0.0
        best_method = "none"
        for dt, engine in engines.items():
            for t in engine.targets:
                if t.fieldname == "name":
                    continue
                score, method = engine._score(header, t)
                if score > best_score:
                    best_doctype, best_target, best_score, best_method = dt, t, score, method

        entry: dict = {
            "header": header,
            "non_empty": round(profile.non_empty, 3) if profile else 0.0,
            "sample": (profile.sample[:5] if profile else []),
            "inferred_type": (profile.inferred_type if profile else "text"),
        }
        if best_target is not None and best_score >= 0.7:
            entry.update({
                "best_match": {
                    "doctype": best_doctype,
                    "target": best_target.qualified,
                    "label": best_target.label,
                    "score": round(best_score, 3),
                    "method": best_method,
                },
                "resolution": "extend_contract",
            })
            flat_target = f"{best_doctype.lower()}.{best_target.qualified}"
            conflicts.append({
                "kind": "unmapped_column",
                "severity": "error",
                "source": header,
                "target": best_target.qualified,
                "doctype": best_doctype,
                "detail": (
                    f"Column '{header}' is not in the flat contract but matches "
                    f"{best_doctype}.{best_target.qualified} "
                    f"({best_method}, score {best_score:.2f})."
                ),
                "suggested_action": (
                    f"set_mapping('customers_full', '{header}', '{flat_target}') "
                    "so the values import."
                ),
            })
        else:
            doctype = best_doctype or "Customer"
            fieldname = _snake(header)
            fieldtype = _fieldtype_for(profile)
            entry.update({
                "best_match": None,
                "resolution": "create_custom_field",
                "suggested_doctype": doctype,
            })
            flat_target = f"{doctype.lower()}.{fieldname}"
            conflicts.append({
                "kind": "unmapped_column",
                "severity": "error",
                "source": header,
                "detail": (
                    f"Column '{header}' has no matching field in "
                    "Customer/Contact/Address."
                ),
                "suggested_action": (
                    f"create_field('{doctype}', '{header}', '{fieldtype}') then "
                    f"set_mapping('customers_full', '{header}', '{flat_target}')."
                ),
            })
            suggested.append({
                "source": header,
                "doctype": doctype,
                "fieldname": fieldname,
                "label": header,
                "fieldtype": fieldtype,
                "reason": "unmapped non-empty source column (flat customers_full)",
                "create_command": (
                    f"python3 erpgen.py createfield {doctype} "
                    f"--label '{header}' --fieldtype {fieldtype}"
                ),
            })
        extra_columns.append(entry)

    agent_instructions = (
        "You are the migration-fix agent for a flat customers_full sheet "
        "(Customer + Contact + Address in one file). The columns below are NOT "
        "in the fixed mapping contract and are dropped at import. Resolve EVERY "
        "conflict (all are error-severity, so none may remain before import):\n"
        "- resolution=extend_contract -> set_mapping('customers_full', column, "
        "'<doctype>.<target>') using the suggested target.\n"
        "- resolution=create_custom_field -> first create_field with the "
        "suggested doctype/label/fieldtype, then set_mapping('customers_full', "
        "column, '<doctype>.<fieldname>') using the suggested fieldname.\n"
        "After resolving, re-run map to confirm the conflicts are gone, then "
        "import. Do not import until the analysis has zero conflicts."
    )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "doctype": "customers_full",
        "source": source_path,
        "source_rows": source.n_rows,
        "base_url": base_url,
        "known_mappings": known_mappings,
        "extra_columns": extra_columns,
        "column_profiles": [p.as_dict() for p in source.profiles],
        "conflicts": conflicts,
        "suggested_custom_fields": suggested,
        "agent_instructions": agent_instructions,
    }


