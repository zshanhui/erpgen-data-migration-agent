"""Reusable tool functions for the migration agent.

Each function maps 1:1 to an `erpgen.py` subcommand, so the same code serves
the CLI (called via subprocess) and an in-process agent loop. Keep these thin —
all heavy lifting stays in the client/mapper.
"""
from __future__ import annotations

from typing import Optional

from .analysis import _snake
from .client import ERPNextClient
from .metadata import DoctypeMeta


def get_record(client: ERPNextClient, doctype: str, name: str) -> dict:
    """Fetch a single record.

    Raises ERPNextError (HTTP 404) when the record does not exist.
    """
    return client.get(doctype, name)


def describe_doctype(
    client: ERPNextClient, doctype: str, include_all: bool = False
) -> dict:
    """Summarize a doctype's structure so an agent can construct records.

    Categories the agent needs: required fields, Link targets, child tables,
    fetch_from (read-only) fields, custom-field count, and the id field.
    Custom fields are included (they're merged into the metadata fetch).
    """
    meta = DoctypeMeta.fetch(client, doctype)

    required: list[dict] = []
    links: list[dict] = []
    tables: list[dict] = []
    fetch_from: list[dict] = []
    read_only: list[dict] = []

    for f in meta.fields:
        entry = {"fieldname": f.fieldname, "label": f.label}
        if f.is_table and f.options:
            tables.append({**entry, "child_doctype": f.options})
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
                return {"name": existing[0]["name"], "created": False}
        except Exception:
            pass
    doc = client.insert(doctype, fields)
    return {"name": doc.get("name"), "created": True}


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
