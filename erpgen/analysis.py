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
from .conflicts import (distinct_values, fieldtype_for, link_value_conflict,
                        missing_link_values, suggested_custom_field,
                        unmapped_column_conflict)
from .mapper import MappingEngine, MappingPlan
from .source import SourceTable


def _snake(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip()).strip("_").lower()
    return re.sub(r"_+", "_", s)


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

        if t.meta.is_link and t.meta.options:
            missing = missing_link_values(client, distinct_values(source, m.source),
                                          t.meta.options, existing_cache)
            if missing:
                conflicts.append(link_value_conflict(
                    m.source, [m.target], t.meta.options, missing))

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
        "- required_missing -> supply --defaults or map a source column.\n"
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
    return path
