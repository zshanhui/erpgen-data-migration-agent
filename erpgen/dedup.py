"""Idempotency helpers: dedup mapped payloads against the live target site.

Strategy: the plan knows the doctype's natural key (`id_field` — the autoname
field or `name`). Before any insert we query which keys already exist, split
payloads into to-create / skipped, and hand only the new ones to the loader.
Re-running the same source is then a safe no-op for existing records.
"""
from __future__ import annotations

from typing import Optional

from .client import ERPNextClient
from .logger import RunLogger
from .mapper import MappingPlan
from .source import SourceTable

BATCH = 500  # max names per `in` filter query

# For doctypes whose `name` is a naming-series value, the *natural key* users
# actually dedup on is a field like customer_name / item_code. When the ID
# field is `name`, prefer one of these payload fields if present.
NATURAL_KEYS = (
    "customer_name",
    "supplier_name",
    "item_code",
    "employee_name",
    "lead_name",
    "project_name",
    "party_name",
)

# Per-doctype natural dedup key, used when the document name is FORMAT-generated
# (so `name` is not a stable key — a re-run would duplicate). Specs:
#   "field"           -> top-level payload field
#   "table.field"     -> first row of a child table
#   ("a", "b")        -> composite of top-level fields (joined with "|")
# `source` extracts from the payload; `target` reads existing records' values.
DEDUP_KEYS = {
    "Contact": {"source": "email_ids.email_id", "target": "email_id"},
    "Address": {"source": ("address_title", "address_type"),
                "target": ("address_title", "address_type")},
}


def extract_key(payload: dict, spec) -> str:
    """Extract the dedup key from a payload per a key spec."""
    if isinstance(spec, tuple):
        return "|".join(str(payload.get(f) or "").strip() for f in spec)
    if isinstance(spec, str) and "." in spec:
        table, field = spec.split(".", 1)
        rows = payload.get(table) or []
        if rows and rows[0].get(field) not in (None, ""):
            return str(rows[0].get(field)).strip()
        return ""
    return str(payload.get(spec) or "").strip()


def existing_keys(client: ERPNextClient, doctype: str, spec) -> set[str]:
    """Fetch the set of existing dedup-key values for a doctype."""
    if isinstance(spec, tuple):
        fields = list(spec)
        rows = client.list(doctype, fields=fields, limit=0)
        return {"|".join(str(r.get(f) or "").strip() for f in fields) for r in rows}
    rows = client.list(doctype, fields=[spec], limit=0)
    return {str(r.get(spec)).strip() for r in rows if r.get(spec) not in (None, "")}


def dedup_key_label(doctype: str, plan: MappingPlan, payloads: list[dict]) -> str:
    """Human-readable name of the dedup key for the current run."""
    spec = DEDUP_KEYS.get(doctype)
    if spec:
        src = spec["source"]
        return src if isinstance(src, str) else "+".join(src)
    return resolve_key_field(payloads, plan.id_field or "name")


def resolve_key_field(payloads: list[dict], id_field: str) -> str:
    """Which payload field carries the dedup key.

    If the doctype's ID field is `name` but payloads carry the natural key in a
    named field (e.g. customer_name), use that field for extraction. The
    existence query still filters on `id_field` (name == natural key value for
    these doctypes).
    """
    if id_field != "name":
        return id_field
    payload_keys: set[str] = set()
    for p in payloads:
        payload_keys.update(p.keys())
    for cand in NATURAL_KEYS:
        if cand in payload_keys:
            return cand
    return id_field


def infer_id_column(
    plan: MappingPlan, source: SourceTable, explicit: Optional[str] = None
) -> Optional[str]:
    """Which SOURCE column carries the natural key.

    Order: explicit --id-column > the column mapped to id_field > a column
    mapped to a known natural-key field > ID/Name headers.
    """
    if explicit:
        if source.column_index(explicit) is None:
            return None
        return explicit
    if plan.id_field:
        for m in plan.mappings:
            if m.target and (m.target == plan.id_field or m.target in NATURAL_KEYS):
                return m.source
    for header in ("ID", "Name"):
        if source.column_index(header) is not None:
            return header
    return None


def existing_names(
    client: ERPNextClient,
    doctype: str,
    id_field: str,
    names: list[str],
) -> set[str]:
    """Query the target site for which of the given key values already exist."""
    found: set[str] = set()
    for i in range(0, len(names), BATCH):
        chunk = names[i : i + BATCH]
        rows = client.list(
            doctype, filters=[[id_field, "in", chunk]], fields=[id_field], limit=0
        )
        found.update(str(r.get(id_field)) for r in rows if r.get(id_field))
    return found


def dedup_payloads(
    client: ERPNextClient,
    doctype: str,
    plan: MappingPlan,
    payloads: list[dict],
    id_column: Optional[str] = None,
    logger: Optional[RunLogger] = None,
) -> tuple[list[dict], list[dict]]:
    """Return (to_create, skipped).

    Rows with an empty key are logged as failed (cannot be deduped safely);
    duplicates within the source are skipped; rows whose key already exists on
    the target are skipped. `skipped` entries are payloads plus a
    `__skip_reason`.
    """
    id_field = plan.id_field or "name"
    key_spec = DEDUP_KEYS.get(doctype)
    if key_spec:
        source_spec = key_spec["source"]
        target_spec = key_spec["target"]
    else:
        source_spec = target_spec = resolve_key_field(payloads, id_field)

    seen: set[str] = set()
    candidates: list[tuple[dict, str]] = []  # (payload, key)
    skipped: list[dict] = []
    for p in payloads:
        key = extract_key(p, source_spec)
        if not key:
            if logger:
                logger.row(p.get("__row"), "", "failed",
                           message=f"missing value for key '{dedup_key_label(doctype, plan, payloads)}'")
            continue
        if key in seen:
            skipped.append({**p, "__skip_reason": "duplicate within source"})
            if logger:
                logger.row(p.get("__row"), key, "skipped",
                           message="duplicate within source")
            continue
        seen.add(key)
        candidates.append((p, key))

    if key_spec:
        existing = existing_keys(client, doctype, target_spec)
    else:
        existing = (
            existing_names(client, doctype, id_field, [k for _, k in candidates])
            if candidates
            else set()
        )

    to_create: list[dict] = []
    for p, key in candidates:
        if key in existing:
            skipped.append({**p, "__skip_reason": "already exists"})
            if logger:
                logger.row(p.get("__row"), key, "skipped", docname=key,
                           message="already exists")
        else:
            to_create.append(p)

    return to_create, skipped
