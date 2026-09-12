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
from .mapper import MappingEngine, convert_value
from .metadata import fetch_with_children
from .source import SourceTable

# ---------------------------------------------------------------- party specs
# A flat party sheet has ONE party doctype (Customer/Supplier) plus inline
# contact + address columns. Everything below is derived from this registry, so
# adding a party type is data, not code.
PARTY_SPECS: dict[str, dict] = {
    "Customer": {
        "flow": "customers_full",
        "key": "customer",
        "name_column": "Customer Name",
        "name_field": "customer_name",
        "type_column": "Customer Type",
        "type_field": "customer_type",
        "group_column": "Group",
        "group_field": "customer_group",
        "extra_columns": {"Territory": "territory"},
    },
    "Supplier": {
        "flow": "suppliers_full",
        "key": "supplier",
        "name_column": "Supplier Name",
        "name_field": "supplier_name",
        "type_column": "Supplier Type",
        "type_field": "supplier_type",
        "group_column": "Supplier Group",
        "group_field": "supplier_group",
        "extra_columns": {},
        # Supplier (unlike Customer) has its own `country` field: the sheet's
        # Country column feeds the Address AND the party record.
        "mirror_columns": {"Country": "country"},
    },
}

# inline contact columns -> synthetic payload keys (handled specially)
CONTACT_COLUMNS = {
    "Contact Name": "contact_name",
    "Email": "email",
    "Phone": "phone",
}

# inline address columns -> Address fieldnames
ADDRESS_COLUMNS = {
    "Address Type": "address_type",
    "Address Line 1": "address_line1",
    "Address Line 2": "address_line2",
    "City": "city",
    "State": "state",
    "Postal Code": "pincode",
    "Country": "country",
}


def spec_for(party: str) -> dict:
    try:
        return PARTY_SPECS[party]
    except KeyError:
        raise ValueError(
            f"unknown party type {party!r} (expected one of {', '.join(PARTY_SPECS)})"
        ) from None


def party_for_flow(flow: str) -> Optional[str]:
    """'suppliers_full' -> 'Supplier'; None when the flow is not a party flow."""
    for party, spec in PARTY_SPECS.items():
        if spec["flow"] == flow:
            return party
    return None


def flow_for_party(party: str) -> str:
    """'Supplier' -> 'suppliers_full'."""
    return spec_for(party)["flow"]


def flat_map_for(party: str) -> dict[str, tuple[str, str]]:
    """header -> (payload key, fieldname) for one party type's flat contract."""
    spec = spec_for(party)
    key = spec["key"]
    m: dict[str, tuple[str, str]] = {
        spec["name_column"]: (key, spec["name_field"]),
        spec["type_column"]: (key, spec["type_field"]),
        spec["group_column"]: (key, spec["group_field"]),
    }
    for col, field in (spec["extra_columns"] or {}).items():
        m[col] = (key, field)
    for col, field in CONTACT_COLUMNS.items():
        m[col] = ("contact", field)
    for col, field in ADDRESS_COLUMNS.items():
        m[col] = ("address", field)
    return m


# Customer's contract, kept for callers that inspect the default flat map.
FLAT_MAP = flat_map_for("Customer")


def detect_party_sheet(source: SourceTable) -> Optional[str]:
    """Return the party doctype for a flat party sheet, else None.

    Signature: a '<Party> Name' column AND at least one inline contact or
    address column.
    """
    hs = set(source.headers)
    for party, spec in PARTY_SPECS.items():
        if spec["name_column"] in hs and ("Contact Name" in hs or "Address Line 1" in hs):
            return party
    return None


def is_party_sheet(source: SourceTable) -> bool:
    return detect_party_sheet(source) is not None


# backward-compatible alias (pre-Supplier naming)
is_customers_full_sheet = is_party_sheet


_FLAT_DOCTYPES = {"customer", "supplier", "contact", "address"}
_DT_CANONICAL = {
    "customer": "Customer",
    "supplier": "Supplier",
    "contact": "Contact",
    "address": "Address",
}
_KEY_FOR_DT = {v: k for k, v in _DT_CANONICAL.items()}


def flat_key(doctype: str) -> str:
    """Canonical doctype -> flat target key ('Supplier' -> 'supplier')."""
    return _KEY_FOR_DT.get(doctype, doctype.lower())


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


def load_flat_mappings(path, flow: str = "customers_full") -> dict[str, tuple[str, str]]:
    """Read one flat flow's contract extensions from the overrides file.

    Returns {header: (key, fieldname)} for every mapping under `flow`
    ('customers_full' | 'suppliers_full') whose target parses as
    '<customer|supplier|contact|address>.<fieldname>'.
    """
    from .overrides import load_overrides

    raw = (load_overrides(path).get(flow) or {}).get("mappings") or {}
    out: dict[str, tuple[str, str]] = {}
    for header, target in raw.items():
        parsed = parse_flat_target(target)
        if parsed:
            out[header] = parsed
    return out


def type_maps(client: ERPNextClient, party: str) -> dict[str, dict[str, str]]:
    """{kind: {fieldname: fieldtype}} for a party flow's three doctypes.

    Used to convert raw source strings to the target field's type — without this
    a Check field receiving "Yes"/"true" is coerced to 0 by Frappe (silent data
    loss), and Float/Date/Int columns stay strings.
    """
    kinds = {party: spec_for(party)["key"], "Contact": "contact", "Address": "address"}
    maps: dict[str, dict[str, str]] = {}
    for dt, kind in kinds.items():
        parent, _children = fetch_with_children(client, dt)
        maps[kind] = {f.fieldname: f.fieldtype for f in parent.fields}
    return maps


def build_payloads(
    source: SourceTable,
    party: str = "Customer",
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
    type_map: Optional[dict[str, dict[str, str]]] = None,
) -> list[dict]:
    """Turn each source row into {<party key>, contact, address} payload dicts.

    Links are NOT set here — the importer attaches them with the real document
    name returned by the insert (Customer/Supplier docs are named by the party
    name, but the insert result is authoritative).

    `flat_mappings` (header -> (key, fieldname)) carries resolved
    out-of-contract columns; values are routed into the matching doctype.
    `type_map` (see `type_maps`) drives value conversion per target fieldtype.
    """
    spec = spec_for(party)
    key = spec["key"]
    fmap = flat_map_for(party)
    extra = flat_mappings or {}
    tmap = type_map or {}

    def conv(kind: str, field: str, raw: str):
        """Convert a raw cell to the target field's type (fallback: raw string)."""
        ftype = (tmap.get(kind) or {}).get(field)
        if not ftype:
            return raw
        try:
            return convert_value(raw, ftype)
        except (ValueError, TypeError):
            return raw

    idx = {h: i for i, h in enumerate(source.headers)}
    payloads: list[dict] = []
    for row in source.rows:
        def cell(h):
            i = idx.get(h)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        record = {k: conv(key, k, cell(h))
                  for h, (kind, k) in fmap.items() if kind == key and cell(h)}
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

        address = {k: conv("address", k, cell(h))
                   for h, (kind, k) in fmap.items() if kind == "address" and cell(h)}
        address_type = address.get("address_type") or "Billing"
        address["address_type"] = address_type
        address["address_title"] = f"{cell(spec['name_column'])} - {address_type}"

        # columns the party doctype also carries itself (e.g. Supplier.country)
        for col, field in (spec.get("mirror_columns") or {}).items():
            val = cell(col)
            if val and field not in record:
                record[field] = conv(key, field, val)

        # resolved out-of-contract columns -> route into the right doctype payload
        for header, (kind, field) in extra.items():
            val = cell(header)
            if not val:
                continue
            if kind == key:
                record[field] = conv(kind, field, val)
            elif kind == "contact":
                contact[field] = conv("contact", field, val)
            elif kind == "address":
                address[field] = conv("address", field, val)

        payloads.append({key: record, "contact": contact, "address": address})
    return payloads


def _ensure_doc_link(client: ERPNextClient, doctype: str, name: str,
                     link_doctype: str, link_name: str,
                     apply: bool) -> tuple[str, str]:
    """Append a Dynamic Link row to an existing Contact/Address if missing.

    Returns (status, message):
      ("skipped", "") — already linked (no-op)
      ("linked", "")  — link added (apply) or would be added (dry-run)
      ("failed", msg) — the update errored on apply

    This is what lets ONE Contact/Address be shared across parties: a contact
    already linked to Customer/Acme still receives a Supplier/Steel Ltd row
    rather than being treated as "already present".
    """
    try:
        existing = client.get(doctype, name)
    except Exception as e:
        return ("failed" if apply else "skipped"), (str(e) if apply else "")
    links = list(existing.get("links") or [])
    if any(str(r.get("link_doctype")) == link_doctype
           and str(r.get("link_name")) == link_name for r in links):
        return "skipped", ""
    links.append({"link_doctype": link_doctype, "link_name": link_name})
    if apply:
        try:
            client.update(doctype, name, {"links": links})
        except Exception as e:
            return "failed", str(e)
    return "linked", ""


def _link_or_create(
    client: ERPNextClient,
    doctype: str,
    payload: dict,
    natural_key: str,
    link: dict,
    index: dict[str, Optional[str]],
    seen_links: dict[str, set],
    apply: bool,
    warn,
    row_no: int,
) -> tuple[str, str]:
    """Dedup one Contact/Address by natural key, link-merging on duplicates.

    `index` maps natural key -> document name (None = created earlier in this
    dry run); `seen_links` tracks (link_doctype, link_name) pairs already known
    to be attached, so dry runs predict `linked` vs `skipped` accurately.
    """
    payload = dict(payload)
    lkey = (link["link_doctype"], link["link_name"])
    payload["links"] = [dict(link)]

    if natural_key in index:
        existing = index[natural_key]
        if existing is None:
            seen = seen_links.setdefault(natural_key, set())
            status = "skipped" if lkey in seen else "linked"
            seen.add(lkey)
            return status, ""
        return _ensure_doc_link(client, doctype, existing, *lkey, apply)

    try:
        if apply:
            created = client.insert(doctype, payload)
            index[natural_key] = str(created.get("name") or natural_key)
        else:
            index[natural_key] = None
        seen_links.setdefault(natural_key, set()).add(lkey)
        return "created", ""
    except Exception as e:
        warn(f"row {row_no}: {doctype} '{natural_key}' failed: {e}")
        return "failed", str(e)


def import_flat_parties(
    client: ERPNextClient,
    source: SourceTable,
    party: str = "Customer",
    defaults: Optional[dict] = None,
    apply: bool = False,
    logger: Optional[RunLogger] = None,
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    """Import a flat party sheet (party + inline contact/address), deduped per doctype.

    Contacts dedup by email, addresses by title+type. When a Contact/Address is
    already present — on the site, earlier in this run, or linked to a DIFFERENT
    party type — it receives a Dynamic Link row for this party instead of being
    re-created. That is what lets one Contact serve a Customer and a Supplier.

    Per-row insert failures are logged as `failed` (with a stderr warning) and
    skipped, never aborting the run. A failed record is NOT added to its dedup
    set, so fixing the source and re-running retries it — and re-links it.
    """
    spec = spec_for(party)
    key = spec["key"]
    name_field = spec["name_field"]
    name_column = spec["name_column"]
    defaults = defaults or {}

    # natural party name -> document name (Customer/Supplier docs are named by
    # the party name, but the insert result is authoritative)
    party_by_name: dict[str, str] = {}
    for r in client.list(party, fields=["name", name_field], limit=0):
        nat = str(r.get(name_field) or "").strip()
        if nat:
            party_by_name[nat] = str(r["name"])

    # email -> contact name (None for contacts created within this dry run)
    contact_by_email: dict[str, Optional[str]] = {
        str(r["email_id"]).strip(): str(r["name"])
        for r in client.list("Contact", fields=["name", "email_id"], limit=0)
        if r.get("email_id")
    }
    contact_links: dict[str, set] = {}  # email -> {(link_doctype, link_name)}

    addr_by_key: dict[str, Optional[str]] = {
        f"{r.get('address_title')}|{r.get('address_type')}".strip(): str(r["name"])
        for r in client.list("Address", fields=["name", "address_title", "address_type"], limit=0)
    }
    address_links: dict[str, set] = {}

    counts = {
        key: {"created": 0, "skipped": 0, "failed": 0},
        "contact": {"created": 0, "skipped": 0, "linked": 0, "failed": 0},
        "address": {"created": 0, "skipped": 0, "linked": 0, "failed": 0},
    }

    def warn(msg: str) -> None:
        print(f"WARNING: {msg}", file=sys.stderr)

    tmap = type_maps(client, party)
    for i, p in enumerate(build_payloads(source, party, flat_mappings, tmap), start=2):
        # ---- party (Customer/Supplier) ----
        rec = dict(p[key])
        for k, v in defaults.items():
            rec.setdefault(k, v)
        name = rec.get(name_field, "").strip()
        pstatus = ""
        pmessage = ""
        docname = ""
        if not name:
            counts[key]["failed"] += 1
            pstatus = "failed"
            pmessage = f"missing {name_column}"
            warn(f"row {i}: missing {name_column} (skipped)")
            if logger:
                logger.log(event="row", row=i, doctype=party, name="",
                           status=pstatus, message=pmessage)
            continue
        if name in party_by_name:
            counts[key]["skipped"] += 1
            pstatus = "skipped"
            docname = party_by_name[name]
        else:
            try:
                if apply:
                    created = client.insert(party, rec)
                    docname = str(created.get("name") or name)
                else:
                    docname = name
                party_by_name[name] = docname
                counts[key]["created"] += 1
                pstatus = "created"
            except Exception as e:
                counts[key]["failed"] += 1
                pstatus = "failed"
                pmessage = str(e)
                warn(f"row {i}: {party} '{name}' failed: {e}")
        if logger:
            logger.log(event="row", row=i, doctype=party, name=name,
                       status=pstatus, message=pmessage)
        if pstatus == "failed":
            # contact/address link to a party that does not exist — skip them
            continue

        link = {"link_doctype": party, "link_name": docname}

        # ---- contact ----
        contact = p["contact"]
        email = (contact.get("email_ids") or [{}])[0].get("email_id", "")
        if email:
            cstatus, cmessage = _link_or_create(
                client, "Contact", contact, email, link,
                contact_by_email, contact_links, apply, warn, i,
            )
            counts["contact"][cstatus] += 1
            if logger:
                logger.log(event="row", row=i, doctype="Contact", key=email,
                           status=cstatus, party=docname, message=cmessage)

        # ---- address ----
        addr = p["address"]
        akey = f"{addr.get('address_title')}|{addr.get('address_type')}".strip()
        astatus, amessage = _link_or_create(
            client, "Address", addr, akey, link,
            addr_by_key, address_links, apply, warn, i,
        )
        counts["address"][astatus] += 1
        if logger:
            logger.log(event="row", row=i, doctype="Address",
                       key=addr.get("address_title"), status=astatus, message=amessage)

    return counts


def run_flat_parties_import(
    client: ERPNextClient,
    source: SourceTable,
    party: str = "Customer",
    defaults: Optional[dict] = None,
    apply: bool = False,
    logger: Optional[RunLogger] = None,
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    spec = spec_for(party)
    counts = import_flat_parties(
        client, source, party=party, defaults=defaults, apply=apply, logger=logger,
        flat_mappings=flat_mappings,
    )
    print(f"{spec['flow']} import ({'APPLY' if apply else 'dry run'}):")
    for label, c in counts.items():
        extra = ""
        if c.get("linked"):
            extra += f" | linked {c['linked']}"
        if c.get("failed"):
            extra += f" | failed {c['failed']}"
        print(f"  {label:<10} created {c['created']} | skipped {c['skipped']}{extra}")
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


def _distinct_values(source: SourceTable, header: str, cap: int = 50) -> list[str]:
    idx = source.column_index(header)
    if idx is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for row in source.rows:
        if idx < len(row):
            v = str(row[idx]).strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
                if len(out) >= cap:
                    break
    return out


def _link_value_conflicts(
    client: ERPNextClient,
    source: SourceTable,
    spec: dict,
    fmap: dict,
    flat: dict,
    engines: dict,
) -> list[dict]:
    """`link_value_conflict` for every mapped Link column whose values are absent.

    Without this a mapped-but-missing Link value (e.g. `Payment Terms` ->
    `Supplier.payment_terms` -> Payment Terms Template) only surfaces as a
    per-row import failure, giving the agent nothing actionable. Covers the fixed
    contract (Group/Country), resolved out-of-contract columns and mirrors. One
    conflict per (column, linked doctype).
    """
    key = spec["key"]
    targets: list[tuple[str, str, str]] = []
    for header, (kind, field) in fmap.items():
        if kind == "contact" and field in CONTACT_COLUMNS.values():
            continue  # synthetic contact keys, not real columns
        targets.append((header, kind, field))
    for header, (kind, field) in flat.items():
        targets.append((header, kind, field))
    for header, field in (spec.get("mirror_columns") or {}).items():
        targets.append((header, key, field))

    grouped: dict[tuple[str, str], dict] = {}
    existing_cache: dict[str, set] = {}
    for header, kind, field in targets:
        if header not in source.headers:
            continue
        engine = engines.get(_DT_CANONICAL.get(kind, ""))
        if engine is None:
            continue
        fmeta = engine.parent.get(field)
        if not fmeta or not fmeta.is_link or not fmeta.options:
            continue
        values = _distinct_values(source, header)
        if not values:
            continue
        existing = existing_cache.get(fmeta.options)
        if existing is None:
            try:
                existing = {str(r.get("name")) for r in
                            client.list(fmeta.options, fields=["name"], limit=0)}
            except Exception:
                existing = set()
            existing_cache[fmeta.options] = existing
        missing = [v for v in values if v not in existing]
        if not missing:
            continue
        g = grouped.setdefault((header, fmeta.options), {"targets": [], "missing": []})
        qual = f"{kind}.{field}"
        if qual not in g["targets"]:
            g["targets"].append(qual)
        g["missing"] = sorted(set(g["missing"]) | set(missing))

    out: list[dict] = []
    for (header, linked), g in grouped.items():
        qual_targets = sorted(g["targets"])
        out.append({
            "kind": "link_value_conflict",
            "severity": "error",
            "source": header,
            "target": qual_targets[0],
            "targets": qual_targets,
            "doctype": linked,
            "missing_values": g["missing"][:25],
            "detail": (
                f"{len(g['missing'])} source value(s) for '{header}' do not exist "
                f"in {linked}."
            ),
            "suggested_action": (
                f"create the missing {linked} record(s) with create_record — "
                f"describe_doctype('{linked}') lists the required fields, including "
                "child-table ones — then re-run map."
            ),
        })
    return out


def build_party_sheet_analysis(
    client: ERPNextClient,
    source: SourceTable,
    party: str = "Customer",
    *,
    base_url: str = "",
    source_path: str = "",
    flat_mappings: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    """Build a flat party-sheet mapping analysis (for the LLM agent).

    Columns in the fixed contract are deterministic; every column outside it is
    surfaced as an `unmapped_column` conflict with one of two resolutions: map
    it to an existing field (extend_contract) or create a custom field
    (create_custom_field, with a ready `create_command`). Already-resolved flat
    overrides are treated as in-contract and NOT flagged.
    """
    spec = spec_for(party)
    fmap = flat_map_for(party)
    engines: dict[str, MappingEngine] = {}
    for dt in (party, "Contact", "Address"):
        parent, children = fetch_with_children(client, dt)
        engines[dt] = MappingEngine(parent, children)

    flat = flat_mappings or {}
    known = set(fmap.keys()) | set(flat.keys())
    known_mappings = [
        {"header": h, "doctype": kind, "target": target, "method": "flat_contract"}
        for h, (kind, target) in fmap.items()
        if h in source.headers
    ]
    known_mappings += [
        {"header": h, "doctype": kind, "target": field, "method": "flat_override"}
        for h, (kind, field) in flat.items()
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
            flat_target = f"{flat_key(best_doctype)}.{best_target.qualified}"
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
                    f"set_mapping('{spec['flow']}', '{header}', '{flat_target}') "
                    "so the values import."
                ),
            })
        else:
            doctype = best_doctype or party
            fieldname = _snake(header)
            fieldtype = _fieldtype_for(profile)
            entry.update({
                "best_match": None,
                "resolution": "create_custom_field",
                "suggested_doctype": doctype,
            })
            flat_target = f"{flat_key(doctype)}.{fieldname}"
            conflicts.append({
                "kind": "unmapped_column",
                "severity": "error",
                "source": header,
                "detail": (
                    f"Column '{header}' has no matching field in "
                    f"{party}/Contact/Address."
                ),
                "suggested_action": (
                    f"create_field('{doctype}', '{header}', '{fieldtype}') then "
                    f"set_mapping('{spec['flow']}', '{header}', '{flat_target}')."
                ),
            })
            suggested.append({
                "source": header,
                "doctype": doctype,
                "fieldname": fieldname,
                "label": header,
                "fieldtype": fieldtype,
                "reason": f"unmapped non-empty source column ({spec['flow']})",
                "create_command": (
                    f"python3 erpgen.py createfield {doctype} "
                    f"--label '{header}' --fieldtype {fieldtype}"
                ),
            })
        extra_columns.append(entry)

    # ---- link values: every mapped Link column must point at records that
    # exist, or the import fails row-by-row (e.g. Supplier.payment_terms ->
    # Payment Terms Template). Checked for contract AND resolved columns.
    conflicts.extend(_link_value_conflicts(client, source, spec, fmap, flat, engines))

    flow = spec["flow"]
    agent_instructions = (
        f"You are the migration-fix agent for a flat {flow} sheet "
        f"({party} + Contact + Address in one file). Columns NOT in the fixed "
        "mapping contract are dropped at import. Resolve EVERY conflict (all are "
        "error-severity, so none may remain before import):\n"
        f"- resolution=extend_contract -> set_mapping('{flow}', column, "
        "'<doctype>.<target>') using the suggested target.\n"
        "- resolution=create_custom_field -> first create_field with the "
        f"suggested doctype/label/fieldtype, then set_mapping('{flow}', "
        "column, '<doctype>.<fieldname>') using the suggested fieldname.\n"
        "- link_value_conflict -> the column is mapped, but some of its values "
        "do not exist in the linked doctype. Create those records with "
        "create_record (describe_doctype shows the required fields, including "
        "child-table fields), then re-run map.\n"
        "After resolving, re-run map to confirm the conflicts are gone, then "
        "import. Do not import until the analysis has zero conflicts."
    )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "doctype": flow,
        "party": party,
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


