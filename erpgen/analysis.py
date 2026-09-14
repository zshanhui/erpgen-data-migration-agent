"""Analysis artifact: the mapping analysis in an LLM-agent-consumable form.

`build_analysis()` packages everything a downstream agent needs to act on:
  * the full mapping plan (incl. equally-scored alternatives)
  * source column profiles (persisted for the first time)
  * structured `conflicts` — unmapped columns, ambiguous mappings, fetch_from
    fields, missing required fields, and Link values that don't exist on the
    target site (real data-conflict detection)
  * `suggested_custom_fields` for unmapped columns, each with a ready-to-run
    `create_command`
  * `agent_instructions` — a directive block an LLM agent can ingest

Saved by `map` and `import` to analysis/analysis-<doctype>-<timestamp>.json.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import ERPNextClient
from .conflicts import (LEAF_ONLY_LINKS, data_quality_conflicts, distinct_values,
                        fieldtype_for, group_node_values, link_group_node,
                        link_value_conflict, missing_link_values,
                        possible_duplicate_pairs, possible_duplicate_row_conflict,
                        required_mapped_columns, suggested_custom_field,
                        unmapped_column_conflict)
from .mapper import MappingEngine, MappingPlan
from .source import SourceTable
from .tree import without_self_provided


def _snake(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip()).strip("_").lower()
    return re.sub(r"_+", "_", s)


def _mapped_field(plan: MappingPlan, column: Optional[str]) -> str:
    """The target field a source column maps to, or "" when unmapped.

    Employee's `Emp ID` is the shape this covers: a sheet's real key column that
    the mapper has no field for.
    """
    if not column:
        return ""
    for m in plan.mappings:
        if m.source == column and m.target:
            return m.target
    return ""


def build_analysis(
    client: ERPNextClient,
    source: SourceTable,
    plan: MappingPlan,
    engine: MappingEngine,
    *,
    id_column: Optional[str] = None,
    base_url: str = "",
    source_path: str = "",
) -> dict:
    conflicts: list[dict] = []
    suggested: list[dict] = []
    by_target = {t.qualified: t for t in engine.targets}
    existing_cache: dict[str, set] = {}  # one lookup per linked doctype

    for m in plan.mappings:
        if not m.target:
            idx = source.column_index(m.source)
            profile = source.profiles[idx] if idx is not None and idx < len(source.profiles) else None
            if profile and profile.non_empty > 0:
                conflicts.append(unmapped_column_conflict(m.source))
                suggested.append(suggested_custom_field(
                    m.source, plan.doctype, _snake(m.source),
                    fieldtype=fieldtype_for(profile),
                    reason="unmapped non-empty source column",
                ))
            continue

        t = by_target.get(m.target)
        if not t:
            continue

        if m.alternatives:
            conflicts.append({
                "kind": "ambiguous_mapping",
                "severity": "warning",
                "source": m.source,
                "target": m.target,
                "alternatives": m.alternatives,
                "detail": f"'{m.source}' scores equally for {', '.join([m.target] + m.alternatives)}.",
                "suggested_action": "confirm which target field is intended.",
            })

        if t.meta.is_fetch_field:
            conflicts.append({
                "kind": "fetch_from",
                "severity": "warning",
                "source": m.source,
                "target": m.target,
                "fetch_from": t.meta.fetch_from,
                "detail": f"Read-only field; populated from {t.meta.fetch_from}. Direct writes are discarded.",
                "suggested_action": "write the value on the source doc (the linked doctype named in fetch_from).",
            })

        # links_to_doctype, NOT is_link+options: a Dynamic Link keeps a sibling
        # FIELD name in `options` (Contact.links.link_name -> "link_doctype"), so
        # treating it as a doctype reports every value as missing — an
        # unresolvable conflict the agent loops on forever.
        linked = t.meta.links_to_doctype
        if linked:
            values = distinct_values(source, m.source)
            missing = missing_link_values(client, values, linked, existing_cache)
            # a self-referencing sheet (Customer Group's parent_customer_group)
            # creates its own parents, so those values are not missing
            if missing:
                missing = without_self_provided(missing, linked, source, plan)
            if missing:
                conflicts.append(link_value_conflict(
                    m.source, [m.target], linked, missing))

            # present but unusable: ERPNext throws when a leaf-only field is given
            # a Group node, so catch it here instead of failing every such row
            # mid-import
            if (plan.doctype, m.target) in LEAF_ONLY_LINKS:
                nodes = group_node_values(client, values, linked, existing_cache)
                if nodes:
                    conflicts.append(link_group_node(
                        m.source, [m.target], linked, nodes))

    # ---- cleaning stage: duplicate keys and empty required/key values -------
    # pure and offline, so it costs nothing beyond the scan
    conflicts.extend(data_quality_conflicts(
        source,
        key_column=id_column,
        key_field=_mapped_field(plan, id_column),
        required=required_mapped_columns(engine.parent, plan),
        compare_columns=[m.source for m in plan.mappings if m.target],
    ))

    # ---- near duplicates: review-only, never a gate ------------------------
    if id_column:
        pairs, pair_count, skipped = possible_duplicate_pairs(
            source, id_column,
            compare_columns=[m.source for m in plan.mappings if m.target],
        )
        if pair_count:
            conflicts.append(possible_duplicate_row_conflict(
                id_column, pairs, pair_count, _mapped_field(plan, id_column), skipped))

    covered = set(plan.mapped_fields()) | set(plan.defaults.keys())
    for f in engine.parent.mandatory_fields():
        if f.fieldname not in covered and not f.is_fetch_field:
            conflicts.append({
                "kind": "required_missing",
                "severity": "error",
                "field": f.fieldname,
                "label": f.label,
                "detail": f"Required field '{f.fieldname}' has no source column and no default.",
                "suggested_action": "map a source column to it, or supply --defaults.",
            })

    agent_instructions = (
        f"You are the migration-fix agent for ERPNext doctype '{plan.doctype}'. "
        "Act on the conflicts below:\n"
        "- unmapped_column -> run the suggested create_command, then re-run import.\n"
        "- ambiguous_mapping -> choose the correct target among target/alternatives.\n"
        "- fetch_from -> the field is read-only; set the value on its source doc.\n"
        "- link_value_conflict -> create the missing option records or remap values.\n"
        "- link_group_node -> the value exists but is a Group node; the field needs "
        "a leaf, so remap the value onto a leaf instead of creating anything.\n"
        "- required_missing -> supply --defaults or map a source column.\n"
        "- duplicate_row -> resolve the conflicting cells in one row, drop the "
        "duplicate, or point --id-column at a column that is unique per entity. "
        "You cannot invent the missing value yourself.\n"
        "- missing_value -> the cell is empty in the source sheet; record the value "
        "as a worksheet correction, or use --defaults when a constant is legitimate. "
        "You cannot invent the value yourself.\n"
        "- possible_duplicate_row -> review-only (warning): two rows may be the same "
        "entity spelled two ways. Decide per pair and record it as a worksheet "
        "correction (merge_rows, or dismiss_conflict when they are genuinely "
        "separate). Do not block the import on this.\n"
        "Do not import until every 'error'-severity conflict is resolved."
    )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "doctype": plan.doctype,
        "source": source_path,
        "source_rows": source.n_rows,
        "id_field": plan.id_field,
        "id_column": id_column,
        "base_url": base_url,
        "plan": plan.as_dict(),
        "column_profiles": [p.as_dict() for p in source.profiles],
        "conflicts": conflicts,
        "suggested_custom_fields": suggested,
        "agent_instructions": agent_instructions,
    }


def save_analysis(analysis: dict, analysis_dir: str | Path) -> Path:
    d = Path(analysis_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    path = d / f"analysis-{analysis['doctype'].lower().replace(' ', '-')}-{stamp}.json"
    path.write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")
    prune_analyses(d)
    return path


#: Analysis artifacts kept PER DOCTYPE (newest first). Every `map`/`import`
#: writes a new one, so an agent run with several rounds leaves a trail; this
#: bounds it without ever removing the artifact a run is reading.
KEEP_PER_DOCTYPE = 10

#: analysis-<doctype-slug>-<YYYYMMDD>-<HHMMSSffffff>.json
_ANALYSIS_NAME = re.compile(r"^analysis-(?P<slug>.+)-(?P<stamp>\d{8}-\d{12,})\.json$")


def analysis_paths(doctype: str, analysis_dir: str | Path = "analysis") -> list[Path]:
    """Analysis files for **exactly** this doctype, oldest first.

    Matching on the slug prefix is not enough: `analysis-customer-*.json` also
    matches a Customer Group file, and since the timestamp begins with a digit
    while the longer slug continues with a letter, the wrong file sorts last —
    so a prefix match silently returns another doctype's analysis.
    """
    slug = doctype.lower().replace(" ", "-")
    out: list[Path] = []
    for path in Path(analysis_dir).glob("analysis-*.json"):
        m = _ANALYSIS_NAME.match(path.name)
        if m and m.group("slug") == slug:
            out.append(path)
    return sorted(out, key=lambda p: p.name)


def prune_analyses(analysis_dir: str | Path,
                   keep: int = KEEP_PER_DOCTYPE) -> list[Path]:
    """Delete all but the `keep` newest analysis files, per doctype.

    Grouped by doctype, never globally: the agent's convergence loop re-reads
    the newest artifact for its doctype after every round, so pruning must not
    be able to take that file away. Files that don't match the expected name
    are left alone — we never delete something we can't classify.
    """
    by_doctype: dict[str, list[tuple[str, Path]]] = {}
    for path in Path(analysis_dir).glob("analysis-*.json"):
        m = _ANALYSIS_NAME.match(path.name)
        if m:
            by_doctype.setdefault(m.group("slug"), []).append((m.group("stamp"), path))

    removed: list[Path] = []
    for entries in by_doctype.values():
        if len(entries) <= keep:
            continue
        # newest first; sort on the stamp alone, since the fixed-width
        # YYYYMMDD-HHMMSSffffff makes lexical order chronological (and Paths
        # are not orderable, so a stamp tie must not fall through to them).
        for _stamp, path in sorted(entries, key=lambda e: e[0], reverse=True)[keep:]:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass  # losing a prune is not worth failing an import over
    return removed
