"""Metadata-aware mapping engine.

Given a source table (headers + samples) and the target DocType's live metadata,
suggest a column -> field mapping with confidence scores, and flag the things
that bite migration projects:
  * required fields with no source
  * read-only `fetch_from` fields (must be written via their source doc)
  * Link fields (values must exist in the target site)
  * child-table fields (header "Label (Table Ref)")

Deterministic for now; an LLM callback can be dropped into
`MappingEngine.suggest(..., llm=None)` later for hard cases.
"""
from __future__ import annotations

import csv
import difflib
import io
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .metadata import DoctypeMeta, FieldMeta
from .source import SourceTable

# ------------------------------------------------------------------ helpers
def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


# normalized header -> candidate generic fieldnames (doctype-agnostic)
SYNONYMS: dict[str, list[str]] = {
    "name": ["customer_name", "supplier_name", "item_name", "company_name", "employee_name"],
    "email": ["email_id"],
    "emailid": ["email_id"],
    "mail": ["email_id"],
    "phone": ["mobile_no", "phone", "phone_no", "contact_number"],
    "tel": ["mobile_no", "phone"],
    "mobile": ["mobile_no"],
    "contactno": ["mobile_no", "phone"],
    "vat": ["tax_id"],
    "taxid": ["tax_id"],
    "tin": ["tax_id"],
    "gst": ["tax_id"],
    "gstin": ["tax_id"],
    "group": ["customer_group", "item_group", "supplier_group"],
    "category": ["item_group", "customer_group"],
    "uom": ["stock_uom", "uom"],
    "unit": ["stock_uom", "uom"],
    "rate": ["rate", "price_list_rate", "standard_rate"],
    "price": ["rate", "price_list_rate"],
    "currency": ["default_currency", "currency"],
    "country": ["country"],
    "city": ["city"],
    "zip": ["pincode", "zip_code"],
    "postalcode": ["pincode", "zip_code"],
    "website": ["website"],
    "web": ["website"],
    "address": ["address_line1", "address_line_1"],
    "street": ["address_line1", "address_line_1"],
    "type": ["customer_type", "supplier_type", "item_type"],
    "status": ["status"],
    "disabled": ["disabled"],
    "active": ["disabled"],
    "isactive": ["disabled"],
}


# ------------------------------------------------------------------ targets
@dataclass
class CandidateTarget:
    doctype: str  # doctype the field belongs to (child for child fields)
    table_path: Optional[str]  # parent fieldname of the child table, None for parent fields
    table_label: Optional[str]  # label of the parent Table field (for headers)
    fieldname: str
    label: str
    meta: FieldMeta

    @property
    def qualified(self) -> str:
        return f"{self.table_path}.{self.fieldname}" if self.table_path else self.fieldname


@dataclass
class ColumnMapping:
    source: str
    target: Optional[str]  # qualified fieldname or None
    confidence: float = 0.0
    method: str = "none"  # exact | normalized | token | synonym | fuzzy | none
    notes: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)  # equally-scored rivals

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "notes": self.notes,
            "alternatives": self.alternatives,
        }


@dataclass
class MappingPlan:
    doctype: str
    mappings: list[ColumnMapping] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)  # values applied for unmapped reqd fields
    warnings: list[str] = field(default_factory=list)
    fetch_from_conflicts: list[str] = field(default_factory=list)
    link_fields: list[str] = field(default_factory=list)
    id_field: Optional[str] = None  # natural key used for dedup (name or autoname field)
    value_maps: dict = field(default_factory=dict)  # {field: {from_value: to_value}}

    def mapped_fields(self) -> list[str]:
        return [m.target for m in self.mappings if m.target]

    def as_dict(self) -> dict:
        return {
            "doctype": self.doctype,
            "id_field": self.id_field,
            "mappings": [m.as_dict() for m in self.mappings],
            "defaults": self.defaults,
            "warnings": self.warnings,
            "fetch_from_conflicts": self.fetch_from_conflicts,
            "link_fields": self.link_fields,
            "value_maps": self.value_maps,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MappingPlan":
        plan = cls(doctype=d["doctype"], defaults=d.get("defaults", {}))
        plan.id_field = d.get("id_field")
        plan.warnings = d.get("warnings", [])
        plan.fetch_from_conflicts = d.get("fetch_from_conflicts", [])
        plan.link_fields = d.get("link_fields", [])
        plan.value_maps = d.get("value_maps", {})
        for m in d.get("mappings", []):
            plan.mappings.append(
                ColumnMapping(
                    source=m["source"],
                    target=m.get("target"),
                    confidence=m.get("confidence", 0.0),
                    method=m.get("method", "none"),
                    notes=m.get("notes", []),
                    alternatives=m.get("alternatives", []),
                )
            )
        return plan


# ------------------------------------------------------------------ engine
class MappingEngine:
    def __init__(
        self,
        parent: DoctypeMeta,
        children: Optional[dict[str, DoctypeMeta]] = None,
        doctype_defaults: Optional[dict] = None,
    ) -> None:
        self.parent = parent
        self.children = children or {}
        self.doctype_defaults = doctype_defaults or {}
        self.targets: list[CandidateTarget] = self._build_targets()

    def _build_targets(self) -> list[CandidateTarget]:
        targets: list[CandidateTarget] = []
        layout = {"Section Break", "Column Break", "Tab Break"}
        for f in self.parent.fields:
            if f.is_table or f.fieldtype in layout or f.fieldname in ("name",) or f.fieldname.startswith("__"):
                continue
            targets.append(
                CandidateTarget(self.parent.name, None, None, f.fieldname, f.label, f)
            )
        # For doctypes whose name is caller-supplied (autoname is None, e.g.
        # Contact/Address), expose `name` as a target with frappe's own "ID"
        # label so an explicit ID column maps and dedups correctly.
        if self.parent.autoname is None:
            targets.append(
                CandidateTarget(
                    self.parent.name, None, None, "name", "ID",
                    FieldMeta(fieldname="name", label="ID", fieldtype="Data"),
                )
            )
        for table_field, child_name in self.parent.table_fields():
            child = self.children.get(child_name)
            if not child:
                continue
            tf = self.parent.get(table_field)
            table_label = (tf.label if tf else table_field) or table_field
            for f in child.fields:
                if f.fieldname.startswith("__") or f.is_table or f.fieldtype in layout:
                    continue
                label = f"{f.label} ({table_label})"
                targets.append(
                    CandidateTarget(
                        child_name, table_field, table_label, f.fieldname, label, f
                    )
                )
        return targets

    # ------------------------------------------------------------ scoring
    def _score(self, header: str, t: CandidateTarget) -> tuple[float, str]:
        h = header.strip()
        hn = norm(h)
        if not hn:
            return 0.0, "none"

        if h == t.label or h == t.fieldname:
            return 1.0, "exact"
        if hn == norm(t.label) or hn == norm(t.fieldname):
            return 0.95, "normalized"

        ht = tokens(h)
        lt = tokens(t.label)
        if ht and lt and (set(ht) <= set(lt) or set(lt) <= set(ht)):
            return 0.85, "token"

        if hn in SYNONYMS and t.fieldname in SYNONYMS[hn]:
            return 0.78, "synonym"

        r = difflib.SequenceMatcher(None, hn, norm(t.label)).ratio()
        if r >= 0.85:
            return round(r, 3), "fuzzy"
        r2 = difflib.SequenceMatcher(None, hn, norm(t.fieldname)).ratio()
        if r2 >= 0.85:
            return round(r2, 3), "fuzzy"
        return 0.0, "none"

    # ------------------------------------------------------------ planning
    def suggest(
        self,
        source: SourceTable,
        llm: Optional[Callable[[list, list], dict]] = None,
    ) -> MappingPlan:
        plan = MappingPlan(doctype=self.parent.name, defaults=dict(self.doctype_defaults))
        plan.id_field = self.parent.id_field

        used_targets: set[str] = set()
        for i, header in enumerate(source.headers):
            profile = source.profiles[i] if i < len(source.profiles) else None
            best: Optional[CandidateTarget] = None
            best_score = 0.0
            best_method = "none"
            rivals: list[str] = []
            for t in self.targets:
                if t.qualified in used_targets:
                    continue
                score, method = self._score(header, t)
                if score > best_score:
                    rivals = [best.qualified] if best else []
                    best, best_score, best_method = t, score, method
                elif score and score == best_score and best:
                    rivals.append(t.qualified)
            if best and best_score >= 0.7:
                used_targets.add(best.qualified)
                m = ColumnMapping(header, best.qualified, best_score, best_method)
                m.alternatives = list(rivals)
                if rivals:
                    m.notes.append(f"ambiguous with: {', '.join(rivals)}")
                if best.meta.is_fetch_field:
                    plan.fetch_from_conflicts.append(
                        f"{header} -> {best.qualified}: read-only, fetched from "
                        f"{best.meta.fetch_from}; write the SOURCE doc instead"
                    )
                    m.notes.append(
                        f"read-only fetch_from field ({best.meta.fetch_from}); "
                        "value must be set on the source doc"
                    )
                if best.meta.is_link:
                    plan.link_fields.append(f"{best.qualified} (Link -> {best.meta.options})")
                    m.notes.append(f"Link field; values must exist in {best.meta.options}")
                if best.table_path:
                    m.notes.append(
                        f"child-table column (table '{best.table_path}', header "
                        f"must be '{best.label}')"
                    )
                plan.mappings.append(m)
            else:
                plan.mappings.append(
                    ColumnMapping(header, None, best_score if best else 0.0, "none")
                )
                if profile and profile.non_empty > 0:
                    plan.warnings.append(
                        f"Source column '{header}' has no matching ERPNext field; "
                        "its values will be dropped"
                    )

        # required-field coverage
        covered = set(plan.mapped_fields()) | set(plan.defaults.keys())
        for f in self.parent.mandatory_fields():
            if f.fieldname not in covered and not f.is_fetch_field:
                plan.warnings.append(
                    f"Required field '{f.fieldname}' ({f.label}) has no source column "
                    f"and no default"
                )

        # fetch_from fields not touched by the source
        for f in self.parent.fetch_fields():
            if f.fieldname not in plan.mapped_fields():
                plan.warnings.append(
                    f"Read-only fetch field '{f.fieldname}' left alone (good): "
                    f"populated from {f.fetch_from}"
                )

        return plan

    # ------------------------------------------------------------ payloads
    def build_payloads(
        self,
        source: SourceTable,
        plan: MappingPlan,
    ) -> tuple[list[dict], list[dict]]:
        """Return (parent_payloads, row_errors). Child columns are grouped into
        the parent's child-table field as a list of dicts."""
        parent_map: list[tuple[ColumnMapping, CandidateTarget]] = []
        child_maps: list[tuple[ColumnMapping, CandidateTarget]] = []
        by_target = {t.qualified: t for t in self.targets}
        for m in plan.mappings:
            if not m.target:
                continue
            t = by_target.get(m.target)
            if not t:
                continue
            if t.table_path:
                child_maps.append((m, t))
            else:
                parent_map.append((m, t))

        payloads: list[dict] = []
        errors: list[dict] = []
        for r_idx, row in enumerate(source.rows, start=2):  # row 1 = header
            parent: dict[str, Any] = dict(plan.defaults)
            child_rows: dict[str, dict] = {}
            row_errors: list[str] = []
            for m, t in parent_map:
                idx = source.column_index(m.source)
                if idx is None or idx >= len(row):
                    continue
                raw = row[idx]
                if raw in ("", None):
                    continue
                try:
                    parent[t.fieldname] = convert_value(raw, t.meta.fieldtype)
                except ValueError as e:
                    row_errors.append(f"{m.source}: {e}")
            for m, t in child_maps:
                idx = source.column_index(m.source)
                if idx is None or idx >= len(row):
                    continue
                raw = row[idx]
                if raw in ("", None):
                    continue
                try:
                    val = convert_value(raw, t.meta.fieldtype)
                except ValueError as e:
                    row_errors.append(f"{m.source}: {e}")
                    continue
                # merge all child columns for a table into ONE row per parent
                child_rows.setdefault(t.table_path, {})[t.fieldname] = val
            # drop fetch_from fields from payloads (can't be written)
            for m, t in parent_map:
                if t.meta.is_fetch_field:
                    parent.pop(t.fieldname, None)
            if row_errors:
                errors.append({"row": r_idx, "errors": row_errors})
                continue
            for table_path, row in child_rows.items():
                parent[table_path] = [row]
            # apply value remaps (overrides value_maps) to parent and child fields
            for key, mapping in (plan.value_maps or {}).items():
                if "." in key:
                    table_path, fieldname = key.split(".", 1)
                    for row in parent.get(table_path) or []:
                        if fieldname in row and str(row[fieldname]) in mapping:
                            row[fieldname] = mapping[str(row[fieldname])]
                else:
                    if key in parent and str(parent[key]) in mapping:
                        parent[key] = mapping[str(parent[key])]
            parent["__row"] = r_idx  # source row number, for logging/dedup
            payloads.append(parent)
        return payloads, errors

    # ------------------------------------------------------------ template
    def build_template_csv(self, plan: MappingPlan, payloads: list[dict]) -> str:
        """CSV with Data-Import-style LABEL headers (one row per parent doc,
        child columns inline as 'Label (Table Ref)')."""
        by_target = {t.qualified: t for t in self.targets}
        cols: list[tuple[str, str]] = []  # (header, qualified)
        for m in plan.mappings:
            if m.target and by_target.get(m.target):
                t = by_target[m.target]
                if t.table_path:
                    header = f"{t.meta.label} ({t.table_label})"
                else:
                    header = t.meta.label
                cols.append((header, m.target))

        out = io.StringIO()
        w = csv.writer(out, quoting=csv.QUOTE_ALL)
        w.writerow([c[0] for c in cols])
        for p in payloads:
            row = []
            for _, q in cols:
                if "." in q:
                    table_path, fieldname = q.split(".", 1)
                    children = p.get(table_path) or []
                    vals = [str(c.get(fieldname, "")) for c in children]
                    row.append(", ".join(vals))
                else:
                    row.append("" if p.get(q) is None else str(p.get(q)))
            w.writerow(row)
        return out.getvalue()


# ------------------------------------------------------------ value conversion
INT_CLEAN = re.compile(r"[^-\d]")
NUM_CLEAN = re.compile(r"[^\d.,\-]")


def convert_value(raw: Any, fieldtype: str) -> Any:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if fieldtype in ("Int", "Check"):
            return int(raw)
        if fieldtype in ("Float", "Currency", "Percent"):
            return float(raw)
        return raw
    s = str(raw).strip()
    if not s:
        return None

    if fieldtype in ("Int", "Check"):
        low = s.lower()
        if low in ("yes", "true", "y", "1"):
            return 1
        if low in ("no", "false", "n", "0"):
            return 0
        return int(INT_CLEAN.sub("", s)) if INT_CLEAN.sub("", s) else 0
    if fieldtype in ("Float", "Currency", "Percent"):
        return _parse_number(s)
    if fieldtype == "Date":
        return _parse_date(s)
    if fieldtype == "Datetime":
        s = s.replace("T", " ")
        if " " in s:
            return _parse_date(s.split(" ")[0]) + " " + s.split(" ")[1]
        return _parse_date(s)
    return s


def _parse_number(s: str) -> float:
    s = s.strip().replace(" ", "")
    # "$1,234.50" / "1.234,50" / "1,234" / "-1 234,5"
    if "," in s and "." in s:
        # last separator is the decimal separator
        if s.rindex(",") > s.rindex("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # ambiguous: 1,234 (thousands) vs 1,23 (decimal). Use token count.
        tail = s.rsplit(",", 1)[1]
        if len(tail) == 2 and not s.startswith("0"):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    return float(NUM_CLEAN.sub("", s))


def _parse_date(s: str) -> str:
    from .source import DATE_PATTERNS

    s = s.strip()
    for pat, fmt in DATE_PATTERNS:
        if pat.match(s):
            from datetime import datetime

            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
    raise ValueError(f"unrecognized date format: {s!r}")
