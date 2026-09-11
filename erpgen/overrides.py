"""Mapping overrides: persistent, reviewable agent decisions.

The overrides file (default `mapping-overrides.json`) stores per-doctype:

    mappings   {source column: target field}   — forced targets (set-mapping)
    defaults   {field: value}                  — fills required fields (set-default, future)
    value_maps {field: {from: to}}             — value remaps (set-value-map, future)

`map`/`import --overrides <file>` apply them on top of the scored plan, so the
source file is never modified and every decision is reproducible/reviewable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .mapper import ColumnMapping, MappingEngine, MappingPlan
from .source import SourceTable

DEFAULT_OVERRIDES = "mapping-overrides.json"


def load_overrides(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"overrides file {p} is not valid JSON: {e}") from e


def save_overrides(path: str | Path, data: dict) -> None:
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _doc_block(data: dict, doctype: str) -> dict:
    return data.setdefault(
        doctype, {"mappings": {}, "defaults": {}, "value_maps": {}}
    )


def set_mapping(
    path: str | Path, doctype: str, column: str, target: str
) -> dict:
    """Record `source column -> target field` in the overrides file."""
    data = load_overrides(path)
    block = _doc_block(data, doctype)
    block["mappings"][column] = target
    save_overrides(path, data)
    return block


def unset_mapping(path: str | Path, doctype: str, column: str) -> bool:
    """Remove a mapping override. Returns True if something was removed."""
    data = load_overrides(path)
    block = data.get(doctype)
    if block and column in block.get("mappings", {}):
        del block["mappings"][column]
        save_overrides(path, data)
        return True
    return False


def apply_overrides(
    plan: MappingPlan,
    source: SourceTable,
    engine: MappingEngine,
    overrides: dict,
) -> int:
    """Apply one doctype's overrides to the plan in place.

    Forced mappings win over scoring (method becomes 'override'); unknown
    source columns / target fields are ignored with a warning. Returns the
    number of mapping changes applied.
    """
    by_target = {t.qualified: t for t in engine.targets}
    count = 0

    for column, target in (overrides.get("mappings") or {}).items():
        if source.column_index(column) is None:
            plan.warnings.append(
                f"Override for unknown source column '{column}' ignored (not in source)."
            )
            continue
        if target not in by_target:
            plan.warnings.append(
                f"Override target '{target}' not found on {plan.doctype} (ignored). "
                "Use createfield to add the field first."
            )
            continue
        mapping = next((m for m in plan.mappings if m.source == column), None)
        if mapping is None:
            mapping = ColumnMapping(column, None, 0.0, "none")
            plan.mappings.append(mapping)
        mapping.target = target
        mapping.confidence = 1.0
        mapping.method = "override"
        mapping.alternatives = []
        mapping.notes = [
            n for n in mapping.notes if not n.startswith("ambiguous with")
        ]
        count += 1

    plan.defaults.update(overrides.get("defaults") or {})
    plan.value_maps = dict(overrides.get("value_maps") or {})

    # an override can satisfy (or uncover) a required field — recompute warnings
    # so they describe the effective plan, not the pre-override scoring
    engine.check_coverage(plan)

    return count
