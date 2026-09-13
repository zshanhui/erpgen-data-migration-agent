"""DocType metadata model, parsed from the live site's DocType docs.

This is the mapper's ground truth: fieldtype, reqd, read_only, fetch_from,
options (Link targets / Table children) are all discoverable at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .client import ERPNextClient


@dataclass
class FieldMeta:
    fieldname: str
    label: str
    fieldtype: str
    reqd: bool = False
    read_only: bool = False
    fetch_from: Optional[str] = None
    options: Optional[str] = None
    parent: Optional[str] = None  # parent doctype for child-doctype fields
    is_virtual: bool = False
    description: str = ""

    @property
    def is_table(self) -> bool:
        return self.fieldtype == "Table"

    @property
    def is_link(self) -> bool:
        return self.fieldtype in ("Link", "Dynamic Link")

    @property
    def is_dynamic_link(self) -> bool:
        """A Dynamic Link's target doctype is a *sibling field's* value.

        `Contact.links.link_name` has `options == "link_doctype"` — a fieldname,
        not a doctype — so its values cannot be validated against the site.
        """
        return self.fieldtype == "Dynamic Link"

    @property
    def links_to_doctype(self) -> Optional[str]:
        """The doctype this field links to, or None when unknowable statically.

        Always prefer this over `is_link` + `options`: for a Dynamic Link,
        `options` is a field reference, and querying it as a doctype silently
        "loses" every row's value and invents an unresolvable conflict.
        """
        if self.fieldtype != "Link":
            return None
        return self.options or None

    @property
    def is_fetch_field(self) -> bool:
        return bool(self.fetch_from)

    def as_dict(self) -> dict:
        return {
            "fieldname": self.fieldname,
            "label": self.label,
            "fieldtype": self.fieldtype,
            "reqd": self.reqd,
            "read_only": self.read_only,
            "fetch_from": self.fetch_from,
            "options": self.options,
            "is_virtual": self.is_virtual,
        }


@dataclass
class DoctypeMeta:
    name: str
    istable: bool = False
    is_submittable: bool = False
    autoname: Optional[str] = None
    naming_series: Optional[str] = None
    fields: list[FieldMeta] = field(default_factory=list)
    _index: dict[str, FieldMeta] = field(default_factory=dict)

    def get(self, fieldname: str) -> Optional[FieldMeta]:
        return self._index.get(fieldname)

    @property
    def id_field(self) -> str:
        """The natural key field: the autoname field (field:xxx) or `name`."""
        if self.autoname and self.autoname.startswith("field:"):
            return self.autoname[len("field:"):]
        return "name"

    def table_fields(self) -> list[tuple[str, str]]:
        """(fieldname, child_doctype) for each Table field."""
        return [(f.fieldname, f.options) for f in self.fields if f.is_table and f.options]

    def link_fields(self) -> list[FieldMeta]:
        return [f for f in self.fields if f.is_link]

    def writable_fields(self) -> list[FieldMeta]:
        return [f for f in self.fields if not f.read_only and not f.is_virtual and not f.is_table]

    def mandatory_fields(self) -> list[FieldMeta]:
        return [f for f in self.writable_fields() if f.reqd]

    def fetch_fields(self) -> list[FieldMeta]:
        """Read-only fields populated from another doc (fetch_from)."""
        return [f for f in self.fields if f.is_fetch_field]

    @classmethod
    def from_api(cls, raw: dict) -> "DoctypeMeta":
        meta = cls(
            name=raw.get("name"),
            istable=bool(raw.get("istable")),
            is_submittable=bool(raw.get("is_submittable")),
            autoname=raw.get("autoname"),
            naming_series=raw.get("naming_series"),
        )
        for f in raw.get("fields", []):
            fm = FieldMeta(
                fieldname=f.get("fieldname", ""),
                label=f.get("label") or f.get("fieldname", ""),
                fieldtype=f.get("fieldtype", "Data"),
                reqd=bool(f.get("reqd")),
                read_only=bool(f.get("read_only")),
                fetch_from=f.get("fetch_from"),
                options=f.get("options"),
                parent=f.get("parent"),
                is_virtual=bool(f.get("is_virtual")),
                description=f.get("description", "") or "",
            )
            if not fm.fieldname:
                continue
            meta.fields.append(fm)
            meta._index[fm.fieldname] = fm
        return meta

    @classmethod
    def fetch(cls, client: ERPNextClient, doctype: str) -> "DoctypeMeta":
        """Fetch a doctype's meta AND merge its Custom Fields.

        The DocType resource response only contains the base schema; custom
        fields live in the `Custom Field` doctype and are merged here so the
        mapper sees the full, real column set (incl. fields added via API).
        """
        meta = cls.from_api(client.doctype_meta(doctype))
        try:
            cfs = client.list(
                "Custom Field",
                filters=[["dt", "=", doctype]],
                fields=["fieldname", "label", "fieldtype", "reqd", "read_only",
                        "fetch_from", "options"],
                limit=0,
            )
        except Exception:
            cfs = []
        for f in cfs:
            fieldname = f.get("fieldname")
            if not fieldname or meta.get(fieldname):
                continue
            fm = FieldMeta(
                fieldname=fieldname,
                label=f.get("label") or fieldname,
                fieldtype=f.get("fieldtype") or "Data",
                reqd=bool(f.get("reqd")),
                read_only=bool(f.get("read_only")),
                fetch_from=f.get("fetch_from"),
                options=f.get("options"),
            )
            meta.fields.append(fm)
            meta._index[fieldname] = fm
        return meta


def fetch_with_children(
    client: ERPNextClient, doctype: str
) -> tuple[DoctypeMeta, dict[str, DoctypeMeta]]:
    """Parent meta + meta of every child doctype referenced by Table fields."""
    parent = DoctypeMeta.fetch(client, doctype)
    children: dict[str, DoctypeMeta] = {}
    for _, child_name in parent.table_fields():
        if child_name and child_name not in children:
            children[child_name] = DoctypeMeta.fetch(client, child_name)
    return parent, children
