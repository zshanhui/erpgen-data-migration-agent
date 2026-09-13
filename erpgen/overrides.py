"""Mapping overrides: persistent, reviewable agent decisions.

The overrides file (default `mapping-overrides.json`) stores per-doctype:

    mappings   {source column: target field}   — forced targets (set-mapping)
    defaults   {field: value}                  — fills required fields (set-default, future)
    value_maps {field: {from: to}}             — value remaps (set-value-map, future)

`map`/`import --overrides <file>` apply them on top of the scored plan, so the
source file is never modified and every decision is reproducible/reviewable.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .mapper import ColumnMapping, MappingEngine, MappingPlan
from .source import SourceTable

DEFAULT_OVERRIDES = "mapping-overrides.json"


def load_overrides(path: str | Path) -> dict:
    """Read the overrides file, failing loudly and usefully when it is corrupt.

    Silently ignoring a corrupt file would drop the user's/agent's decisions and
    silently produce wrong mappings, so this raises — but says exactly where the
    problem is and how to recover.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"overrides file {p} is not valid JSON: {e}\n"
            f"  at line {e.lineno}, column {e.colno}\n"
            "  Fix that line, or delete the file to start over — the agent will "
            "re-derive the decisions."
        ) from e


def save_overrides(path: str | Path, data: dict) -> None:
    """Write the overrides file atomically (temp file + rename).

    A plain `write_text` can leave a truncated or half-written file if the
    process dies mid-write, and readers then hard-fail on invalid JSON — which
    blocks the entire migration. Rename is atomic on the same filesystem.

    The temp file name must be unique per writer: the agent issues parallel
    tool calls, so two savers sharing one temp path either interleave into it
    (publishing invalid JSON) or lose the file to the other's rename.
    """
    p = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp_name, p)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(path: str | Path):
    """Serialise one read-modify-write of the overrides file.

    `set_mapping`/`unset_mapping` are load -> modify -> save, and the agent
    fires them as parallel tool calls: without this, the second writer
    publishes a snapshot taken before the first decision landed and silently
    drops it. flock covers threads and processes alike; the lock is a sibling
    file because `os.replace` swaps the overrides file's inode, so locking the
    file itself would not exclude anyone.
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _doc_block(data: dict, doctype: str) -> dict:
    return data.setdefault(
        doctype, {"mappings": {}, "defaults": {}, "value_maps": {}}
    )


def set_mapping(
    path: str | Path, doctype: str, column: str, target: str
) -> dict:
    """Record `source column -> target field` in the overrides file."""
    with _locked(path):
        data = load_overrides(path)
        block = _doc_block(data, doctype)
        block["mappings"][column] = target
        save_overrides(path, data)
    return block


def unset_mapping(path: str | Path, doctype: str, column: str) -> bool:
    """Remove a mapping override. Returns True if something was removed."""
    with _locked(path):
        data = load_overrides(path)
        block = data.get(doctype)
        if not (block and column in block.get("mappings", {})):
            return False
        del block["mappings"][column]
        save_overrides(path, data)
        return True


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
