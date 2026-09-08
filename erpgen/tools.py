"""Reusable tool functions for the migration agent.

Each function maps 1:1 to an `erpgen.py` subcommand, so the same code serves
the CLI (called via subprocess) and an in-process agent loop. Keep these thin —
all heavy lifting stays in the client/mapper.
"""
from __future__ import annotations

import json
from pathlib import Path
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


def parse_import_log(path: str | Path) -> tuple[str, list[str]]:
    """Read an import run log and return (doctype, ordered created docnames).

    The import logger writes one JSON object per line; created rows carry
    `event == "row"` and `status == "created"` with their docname.
    """
    doctype = None
    names: list[str] = []
    seen: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "run_start":
            doctype = entry.get("doctype") or doctype
        elif entry.get("event") == "row" and entry.get("status") == "created":
            name = entry.get("docname")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    if not doctype:
        raise ValueError(f"no run_start/doctype found in {path}")
    return doctype, names


def rollback_records(
    client: ERPNextClient,
    doctype: str,
    names: list[str],
    apply: bool = False,
) -> dict:
    """Delete the records a migration created, newest-first.

    `apply=False` returns a dry-run preview without touching anything.
    Deletions that fail (e.g. the record is now referenced elsewhere) are
    collected with their error message rather than aborting the rollback.
    """
    deleted: list[str] = []
    failed: list[dict] = []
    for name in reversed(names):
        if not apply:
            deleted.append(name)
            continue
        try:
            client.delete(doctype, name)
            deleted.append(name)
        except Exception as e:  # noqa: BLE001 — referenced/absent records reported
            failed.append({"name": name, "error": str(e)})
    return {
        "doctype": doctype,
        "total": len(names),
        "deleted": len(deleted),
        "failed": failed,
    }


def parse_agent_schema_changes(path: str | Path) -> list[dict]:
    """Extract custom fields the agent created, from a transcript log.

    Reads `tool_result` lines where the `create_field` tool returned
    `created: true`; the result carries the Custom Field doc name (dropping it
    drops the column).
    """
    fields: list[dict] = []
    seen: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") != "tool_result" or entry.get("tool") != "create_field":
            continue
        try:
            result = json.loads(entry.get("result_tail") or "{}")
        except json.JSONDecodeError:
            continue
        name = result.get("name")
        if result.get("created") and name and name not in seen:
            seen.add(name)
            fields.append({"custom_field": name, "fieldname": result.get("fieldname")})
    return fields


def rollback_schema(client: ERPNextClient, fields: list[dict],
                    apply: bool = False) -> dict:
    """Drop the custom fields the agent created (DROPS the columns + data).

    Dry-run by default; `apply=True` deletes each Custom Field doc.
    """
    deleted: list[dict] = []
    failed: list[dict] = []
    for f in fields:
        if not apply:
            deleted.append(f)
            continue
        try:
            client.delete("Custom Field", f["custom_field"])
            deleted.append(f)
        except Exception as e:  # noqa: BLE001
            failed.append({"custom_field": f["custom_field"], "error": str(e)})
    return {"total": len(fields), "deleted": len(deleted), "failed": failed}


def parse_agent_option_records(path: str | Path) -> list[dict]:
    """Extract lookup records the agent created via create_record.

    Returns [{"doctype", "name"}] pairs, deduped, in transcript order.
    """
    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") != "tool_result" or entry.get("tool") != "create_record":
            continue
        try:
            args = json.loads(entry.get("args") or "{}")
            result = json.loads(entry.get("result_tail") or "{}")
        except json.JSONDecodeError:
            continue
        doctype, name = args.get("doctype"), result.get("name")
        if not doctype or not name or not result.get("created"):
            continue
        key = (doctype, name)
        if key not in seen:
            seen.add(key)
            records.append({"doctype": doctype, "name": name})
    return records


def rollback_options(client: ERPNextClient, records: list[dict],
                     apply: bool = False) -> dict:
    """Delete the lookup records the agent created, newest-first."""
    deleted: list[dict] = []
    failed: list[dict] = []
    for r in reversed(records):
        if not apply:
            deleted.append(r)
            continue
        try:
            client.delete(r["doctype"], r["name"])
            deleted.append(r)
        except Exception as e:  # noqa: BLE001
            failed.append({**r, "error": str(e)})
    return {"total": len(records), "deleted": len(deleted), "failed": failed}
