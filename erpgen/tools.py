"""Reusable tool functions for the migration agent.

Each function maps 1:1 to an `erpgen.py` subcommand, so the same code serves
the CLI (called via subprocess) and an in-process agent loop. Keep these thin —
all heavy lifting stays in the client/mapper.
"""
from __future__ import annotations

from typing import Optional

from .analysis import _snake
from .client import ERPNextClient

# When set (e.g. by an agent run), create_field / create_record journal their
# effects so a whole run can be reverted with `erpgen.py revert`.
ACTIVE_JOURNAL = None
from .metadata import DoctypeMeta, fetch_with_children


def get_record(client: ERPNextClient, doctype: str, name: str) -> dict:
    """Fetch a single record.

    Raises ERPNextError (HTTP 404) when the record does not exist.
    """
    return client.get(doctype, name)


def describe_doctype(
    client: ERPNextClient, doctype: str, include_all: bool = False
) -> dict:
    """Summarize a doctype's structure so an agent can construct records.

    Categories the agent needs: required fields, Link targets, child tables
    (with each child's own required fields, so an agent can build a valid row),
    fetch_from (read-only) fields, custom-field count, and the id field.
    Custom fields are included (they're merged into the metadata fetch).
    """
    meta, child_metas = fetch_with_children(client, doctype)

    required: list[dict] = []
    links: list[dict] = []
    tables: list[dict] = []
    fetch_from: list[dict] = []
    read_only: list[dict] = []

    for f in meta.fields:
        entry = {"fieldname": f.fieldname, "label": f.label}
        if f.is_table and f.options:
            # a child row is invalid without these, and the agent cannot see them
            # from the parent alone (e.g. Payment Terms Template -> invoice_portion)
            child = child_metas.get(f.options)
            child_required = []
            if child:
                for cf in child.mandatory_fields():
                    item = {"fieldname": cf.fieldname, "label": cf.label,
                            "fieldtype": cf.fieldtype}
                    if cf.fieldtype == "Select" and cf.options:
                        item["options"] = [o for o in cf.options.split("\n") if o.strip()]
                    child_required.append(item)
            tables.append({**entry, "child_doctype": f.options,
                           "required": child_required})
            continue
        if f.is_link:
            links.append({**entry, "doctype": f.options})
        if f.is_fetch_field:
            fetch_from.append({**entry, "fetch_from": f.fetch_from})
        if f.reqd:
            required.append({**entry, "fieldtype": f.fieldtype, "options": f.options})
        if f.read_only:
            read_only.append({**entry, "fieldtype": f.fieldtype})

    try:
        custom_count = len(
            client.list("Custom Field", filters=[["dt", "=", doctype]],
                        fields=["fieldname"], limit=0)
        )
    except Exception:
        custom_count = 0

    result: dict = {
        "doctype": meta.name,
        "istable": meta.istable,
        "is_submittable": meta.is_submittable,
        "autoname": meta.autoname,
        "id_field": meta.id_field,
        "field_count": len(meta.fields),
        "custom_field_count": custom_count,
        "fields": {
            "required": required,
            "links": links,
            "tables": tables,
            "fetch_from": fetch_from,
            "read_only": read_only,
        },
    }
    if include_all:
        result["all_fields"] = [
            {
                "fieldname": f.fieldname,
                "label": f.label,
                "fieldtype": f.fieldtype,
                "reqd": f.reqd,
                "read_only": f.read_only,
                "fetch_from": f.fetch_from,
                "options": f.options,
            }
            for f in meta.fields
        ]
    return result


def create_field(
    client: ERPNextClient,
    doctype: str,
    label: str,
    fieldtype: str = "Data",
    fieldname: Optional[str] = None,
    options: Optional[str] = None,
    reqd: bool = False,
    read_only: bool = False,
    insert_after: Optional[str] = None,
    fetch_from: Optional[str] = None,
    default: Optional[str] = None,
) -> dict:
    """Create a custom field (column) on a doctype. Idempotent.

    Returns {"name", "fieldname", "created": bool} — `created=False` when the
    field already exists.
    """
    fieldname = fieldname or _snake(label)
    if not fieldname:
        raise ValueError(f"cannot derive a fieldname from label {label!r}")
    existing = client.list(
        "Custom Field",
        filters=[["dt", "=", doctype], ["fieldname", "=", fieldname]],
        fields=["name"],
        limit=1,
    )
    if existing:
        # the column already exists: the requirement is met even though this run
        # creates nothing (no inverse to journal)
        if ACTIVE_JOURNAL is not None and hasattr(ACTIVE_JOURNAL, "note_condition"):
            ACTIVE_JOURNAL.note_condition("custom_field_create", doctype=doctype,
                                          label=label, fieldname=fieldname)
        return {"name": existing[0]["name"], "fieldname": fieldname, "created": False}

    doc: dict = {
        "dt": doctype,
        "label": label,
        "fieldname": fieldname,
        "fieldtype": fieldtype,
    }
    if options:
        doc["options"] = options
    if reqd:
        doc["reqd"] = 1
    if read_only:
        doc["read_only"] = 1
    if insert_after:
        doc["insert_after"] = insert_after
    if fetch_from:
        doc["fetch_from"] = fetch_from
    if default:
        doc["default"] = default

    cf = client.insert("Custom Field", doc)
    if ACTIVE_JOURNAL is not None:
        ACTIVE_JOURNAL.custom_field_created(doctype, fieldname, cf["name"], label=label)
    return {"name": cf["name"], "fieldname": fieldname, "created": True}


def create_record(client: ERPNextClient, doctype: str, fields: dict) -> dict:
    """Create a record in any doctype, deriving the name field from metadata.

    Idempotent: if a record with the same name-field value already exists it is
    returned with `created=False`. Raises ERPNextError on validation failure.
    """
    meta = DoctypeMeta.fetch(client, doctype)
    id_field = meta.id_field
    name_value = fields.get(id_field) or fields.get("name")
    if name_value:
        try:
            existing = client.list(
                doctype,
                filters=[[id_field, "=", str(name_value)]],
                fields=["name"],
                limit=1,
            )
            if existing:
                if ACTIVE_JOURNAL is not None and hasattr(ACTIVE_JOURNAL, "note_condition"):
                    ACTIVE_JOURNAL.note_condition("record_create", doctype=doctype,
                                                  name=existing[0]["name"])
                return {"name": existing[0]["name"], "created": False}
        except Exception:
            pass
    doc = client.insert(doctype, fields)
    if ACTIVE_JOURNAL is not None and doc.get("name"):
        ACTIVE_JOURNAL.record_created(doctype, doc["name"])
    return {"name": doc.get("name"), "created": True}


def update_record(client: ERPNextClient, doctype: str, name: str,
                  fields: dict) -> dict:
    """Change fields on an EXISTING record, journaling what they held before.

    The read comes first so the inverse is exact: only the keys being written are
    captured, and a missing record fails here (404) rather than halfway through.
    `name` is refused — renaming moves links with it and is not a field write.
    """
    if not fields:
        raise ValueError("update_record needs at least one field to write")
    if "name" in fields:
        raise ValueError("update_record cannot rename a record; drop 'name'")
    doc = client.get(doctype, name)
    before = {f: doc.get(f) for f in fields}
    updated = client.update(doctype, name, fields)
    if ACTIVE_JOURNAL is not None:
        ACTIVE_JOURNAL.record_updated(doctype, name, before)
    return {"name": updated.get("name") or name, "updated": sorted(fields)}


def list_records(
    client: ERPNextClient,
    doctype: str,
    filters: Optional[list] = None,
    fields: Optional[list] = None,
    limit: int = 0,
    order_by: Optional[str] = None,
) -> list[dict]:
    """List records, optionally filtered.

    Defaults to `fields=["name"]` so existence checks stay cheap; pass
    `fields=["*"]` for full documents. `filters` is ERPNext filter syntax,
    e.g. [["customer_group", "=", "Commercial"]] or [["name", "like", "%Whol%"]].
    """
    if fields is None:
        fields = ["name"]
    return client.list(
        doctype, filters=filters, fields=fields, limit=limit, order_by=order_by
    )


