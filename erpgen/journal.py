"""Revertible effects: every mutation records the inverse that undoes it.

Instead of re-deriving what a run did by parsing import/agent logs after the
fact, each effect is journaled **at effect time** together with its inverse.
`erpgen.py revert` replays the inverses LIFO — the authoritative undo path.

Journal file: `logs/journal-<doctype>-<timestamp>.jsonl`, one JSON per line:

    {"event": "run_start", "run_id": ..., "doctype": ..., "source": ..., "base_url": ...}
    {"event": "effect", "kind": "record_create",  "inverse": {"op": "delete_record", ...}}
    {"event": "effect", "kind": "custom_field_create", "inverse": {"op": "delete_custom_field", ...}}
    {"event": "effect", "kind": "override_set", "inverse": {"op": "restore_override", ...}}
    {"event": "run_end", "effects": 12}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import ERPNextClient

JOURNAL_PREFIX = "journal-"


class MigrationJournal:
    """Append-only log of {effect, inverse} pairs for one migration run."""

    def __init__(self, log_dir: str | Path, doctype: str, source: str = "",
                 base_url: str = "", run_id: Optional[str] = None) -> None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
        self.run_id = run_id or stamp
        self.doctype = doctype
        self.path = d / f"{JOURNAL_PREFIX}{doctype.lower().replace(' ', '-')}-{stamp}.jsonl"
        self.count = 0
        self._fh = self.path.open("w", encoding="utf-8")
        self._log(event="run_start", run_id=self.run_id, doctype=doctype,
                  source=source, base_url=base_url)

    def _log(self, **entry) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def effect(self, kind: str, inverse: dict, **detail) -> None:
        """Record an applied effect plus the inverse that undoes it."""
        self.count += 1
        self._log(event="effect", kind=kind, inverse=inverse, **detail)

    # ---- typed helpers -------------------------------------------------
    def record_created(self, doctype: str, name: str) -> None:
        self.effect("record_create",
                    {"op": "delete_record", "doctype": doctype, "name": name},
                    doctype=doctype, name=name)

    def custom_field_created(self, doctype: str, fieldname: str,
                             custom_field_name: str, label: str = "") -> None:
        self.effect("custom_field_create",
                    {"op": "delete_custom_field", "name": custom_field_name},
                    doctype=doctype, name=custom_field_name, fieldname=fieldname,
                    label=label or fieldname)

    def override_set(self, doctype: str, column: str, target: Optional[str],
                     previous: Optional[str], overrides_path: str) -> None:
        self.effect("override_set",
                    {"op": "restore_override", "doctype": doctype, "column": column,
                     "previous": previous, "path": overrides_path},
                    doctype=doctype, column=column, target=target)

    def close(self) -> None:
        self._log(event="run_end", run_id=self.run_id, effects=self.count)
        self._fh.close()


# ---- reading / reverting ------------------------------------------------
def parse_journal(path: str | Path) -> dict:
    """Return {"run_start": {...} | None, "effects": [...]} for a journal file."""
    run_start: Optional[dict] = None
    effects: list[dict] = []
    extra: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = entry.get("event")
        if ev == "run_start":
            run_start = entry
        elif ev == "effect":
            effects.append(entry)
        else:
            extra.append(entry)
    return {"run_start": run_start, "effects": effects, "extra": extra}


def describe_inverse(inv: dict) -> str:
    op = inv.get("op")
    if op == "delete_record":
        return f"delete record {inv.get('doctype')}/{inv.get('name')}"
    if op == "delete_custom_field":
        return f"drop custom field {inv.get('name')}  (DROPS COLUMN + DATA)"
    if op == "restore_override":
        prev = inv.get("previous")
        action = f"-> {prev!r}" if prev is not None else "(remove override)"
        return f"restore override {inv.get('doctype')}.{inv.get('column')} {action}"
    return f"unknown inverse op {op!r}"


def apply_inverse(client: ERPNextClient, inv: dict, apply: bool = True) -> tuple[bool, str]:
    """Execute one inverse. Returns (ok, error_message)."""
    op = inv.get("op")
    if not apply:
        return True, ""
    try:
        if op == "delete_record":
            client.delete(inv["doctype"], inv["name"])
            return True, ""
        if op == "delete_custom_field":
            client.delete("Custom Field", inv["name"])
            return True, ""
        if op == "restore_override":
            from .overrides import DEFAULT_OVERRIDES, set_mapping, unset_mapping

            path = inv.get("path") or DEFAULT_OVERRIDES
            if inv.get("previous") is None:
                unset_mapping(path, inv["doctype"], inv["column"])
            else:
                set_mapping(path, inv["doctype"], inv["column"], inv["previous"])
            return True, ""
        return False, f"unknown inverse op {op!r}"
    except Exception as e:  # noqa: BLE001 — collected, never fatal
        msg = str(e)
        # an inverse that deletes something already gone means the effect was
        # undone by other means (or reverted before): treat as a no-op, so
        # reverting twice is safe instead of reporting a spurious failure
        if op in ("delete_record", "delete_custom_field") and \
                ("DoesNotExistError" in msg or "404" in msg):
            return True, ""
        return False, str(e)


def _already_reverted(data: dict) -> Optional[dict]:
    """The latest revert marker in this journal, if it covers all effects."""
    markers = [e for e in data.get("extra", []) if e.get("event") == "revert"]
    if not markers:
        return None
    last = markers[-1]
    if last.get("status") == "ok" and last.get("reverted", 0) >= len(data["effects"]):
        return last
    return None


def mark_reverted(path: str | Path, run_id: str, reverted: int, status: str) -> None:
    """Append a revert marker so re-reverting is detectable and skippable."""
    p = Path(path)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "event": "revert", "run_id": run_id, "reverted": reverted,
                             "status": status}, default=str) + "\n")


def revert_journal(client: ERPNextClient, path: str | Path,
                   apply: bool = False, force: bool = False) -> dict:
    """Replay a journal's inverses LIFO (newest effect first).

    Reverting an already-reverted journal is a no-op unless `force` is set:
    effects are inverses held at effect time, so replaying them twice would
    otherwise try to delete records that are legitimately gone.
    """
    data = parse_journal(path)
    marker = _already_reverted(data)
    if marker and apply and not force:
        return {"path": str(path), "run_start": data["run_start"],
                "total": len(data["effects"]), "applied": 0, "failed": [],
                "already_reverted": marker, "results": []}
    results: list[dict] = []
    for entry in reversed(data["effects"]):
        inv = entry.get("inverse") or {}
        ok, err = apply_inverse(client, inv, apply=apply)
        results.append({
            "kind": entry.get("kind"),
            "description": describe_inverse(inv),
            "ok": ok,
            "error": err,
        })
    applied = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    if apply and not failed and results:
        mark_reverted(path, (data.get("run_start") or {}).get("run_id") or "", applied, "ok")
    return {
        "path": str(path),
        "run_start": data["run_start"],
        "total": len(results),
        "applied": applied,
        "failed": failed,
        "results": results,
    }
