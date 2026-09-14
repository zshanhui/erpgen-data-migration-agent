"""Self-referencing tree sheets: parent-first order and `is_group`.

A sheet importing a tree doctype (Customer Group, Item Group, Supplier Group)
names its own parents: `parent_customer_group` holds Customer Group names that
this same file creates. ERPNext maintains the nested set (lft/rgt) on insert,
but two things are on us:

  * a child inserted before its parent fails with LinkValidationError (HTTP
    417), so the row is logged `failed` and skipped;
  * a parent created with `is_group = 0` becomes a leaf *with* children.
    ERPNext does not reject that, so the tree is silently malformed.

Both are handled here rather than left to the source file's row order.
"""
from __future__ import annotations

from typing import Optional

from .conflicts import distinct_values
from .mapper import MappingEngine, MappingPlan
from .source import SourceTable


def self_link_field(plan: MappingPlan, engine: MappingEngine) -> Optional[str]:
    """The *mapped* field that links back to the doctype being imported.

    These doctypes carry more than one self-link (`old_parent` is a legacy
    duplicate), so this follows the mapping instead of guessing by field name.
    """
    by_target = {t.qualified: t for t in engine.targets}
    for m in plan.mappings:
        if not m.target:
            continue
        t = by_target.get(m.target)
        if t is not None and t.meta.links_to_doctype == plan.doctype:
            return m.target
    return None


def sheet_identity_values(source: SourceTable, plan: MappingPlan) -> set[str]:
    """The names this file creates: values of the column mapped to `id_field`."""
    if not plan.id_field:
        return set()
    for m in plan.mappings:
        if m.target == plan.id_field:
            return {str(v).strip() for v in distinct_values(source, m.source)}
    return set()


def without_self_provided(missing: list[str], linked_doctype: str,
                          source: SourceTable, plan: MappingPlan) -> list[str]:
    """Drop parent values the sheet itself creates.

    Only for a self-link: a `parent_customer_group` value that is a row of this
    same sheet is not missing, it is created here. A parent found in neither the
    sheet nor the site is still a genuine conflict and still reported.
    """
    if not missing or linked_doctype != plan.doctype:
        return missing
    provided = sheet_identity_values(source, plan)
    return [v for v in missing if v not in provided]


def _name(payload: dict, id_field: str) -> str:
    return str(payload.get(id_field) or "").strip()


def _parent_of(payload: dict, parent_field: str) -> str:
    return str(payload.get(parent_field) or "").strip()


def order_payloads(payloads: list[dict], id_field: str,
                   parent_field: str) -> tuple[list[dict], list[str]]:
    """`(parents-first payloads, warnings)`.

    A parent this sheet does not contain is either already on the site or
    blank, so it imposes no order. A cycle — including a row that names itself
    as its own parent — cannot be ordered: those rows keep their source order
    and a warning names them.
    """
    names = {_name(p, id_field) for p in payloads} - {""}
    pending = list(range(len(payloads)))
    ordered: list[dict] = []
    done: set[str] = set()
    warnings: list[str] = []

    while pending:
        ready = [i for i in pending
                 if _parent_of(payloads[i], parent_field) not in (names - done)]
        if not ready:
            stuck = [_name(payloads[i], id_field) for i in pending]
            warnings.append(
                "cannot order row(s) " + ", ".join(stuck[:5])
                + " — a parent/child cycle; source order kept"
            )
            ordered.extend(payloads[i] for i in pending)
            break
        for i in ready:
            done.add(_name(payloads[i], id_field))
        ordered.extend(payloads[i] for i in ready)
        ready_set = set(ready)
        pending = [i for i in pending if i not in ready_set]

    return ordered, warnings


def derive_is_group(payloads: list[dict], id_field: str, parent_field: str,
                    fieldname: str = "is_group") -> int:
    """Flag every row that another row names as its parent.

    Returns how many parent rows we had to mark as groups. A row that already
    carries a value — the sheet has an Is Group column with something in it —
    keeps it; a row whose column is absent *or blank* is derived, because a
    blank column otherwise leaves intermediate nodes as leaves and ERPNext
    accepts that.
    """
    parents = {_parent_of(p, parent_field) for p in payloads} - {""}
    marked = 0
    for p in payloads:
        if fieldname in p:                       # the sheet's own answer wins
            continue
        value = 1 if _name(p, id_field) in parents else 0
        p[fieldname] = value
        marked += value
    return marked


def apply_tree_semantics(engine: MappingEngine, plan: MappingPlan,
                         payloads: list[dict]) -> tuple[list[dict], list[str]]:
    """Parent-first order + `is_group` for a self-referencing sheet.

    Returns `(payloads, warnings)`. A sheet that does not map a self-link comes
    back untouched, so every other import path is unaffected.
    """
    parent_field = self_link_field(plan, engine)
    if not parent_field or not plan.id_field:
        return payloads, []

    warnings: list[str] = []
    has_is_group = any(f.fieldname == "is_group" for f in engine.parent.fields)
    if has_is_group:
        flagged = derive_is_group(payloads, plan.id_field, parent_field)
        if flagged:
            warnings.append(
                f"marked {flagged} parent row(s) is_group — an intermediate node "
                "must be a group, or ERPNext stores it as a leaf with children"
            )

    ordered, order_warnings = order_payloads(payloads, plan.id_field, parent_field)
    warnings.extend(order_warnings)
    return ordered, warnings
