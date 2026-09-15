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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import ERPNextClient

JOURNAL_PREFIX = "journal-"

#: How many journal files to keep on disk. Beyond this the oldest are deleted,
#: but never one whose effects are still un-reverted: that file IS the undo path
#: for changes already applied. `run-*.jsonl` contexts are not pruned — they hold
#: requirements and the audit of a whole `--run` migration.
KEEP_JOURNALS = 20

#: `journal-<doctype-slug>-<YYYYMMDD>-<HHMMSSffffff>.jsonl`
_JOURNAL_NAME = re.compile(r"^journal-(?P<slug>.+)-(?P<stamp>\d{8}-\d{12,})\.jsonl$")


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
        self._closed = False
        # the file is not created until there is an effect to record: a run that
        # changed nothing has nothing to undo, and an empty journal only makes
        # `--latest` point at a file with nothing in it
        self._dir = d
        self._fh = None
        self.pruned: list[Path] = []
        self.retention_kept: list[Path] = []
        self._header = {"event": "run_start", "run_id": self.run_id,
                        "doctype": doctype, "source": source, "base_url": base_url}

    @property
    def created(self) -> bool:
        """True once an effect has been written, i.e. the file exists."""
        return self._fh is not None

    def _write(self, entry: dict) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def _ensure_open(self) -> None:
        """Create the file on first use, with `run_start` ahead of whatever follows."""
        if self._fh is None:
            self._fh = self.path.open("w", encoding="utf-8")
            self._write(self._header)

    def _log(self, **entry) -> None:
        self._ensure_open()
        self._write(entry)

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

    def link_added(self, doctype: str, name: str, link_doctype: str,
                   link_name: str) -> None:
        """A Dynamic Link row added to an EXISTING record (e.g. link-merge)."""
        self.effect("record_link_add",
                    {"op": "remove_record_link", "doctype": doctype, "name": name,
                     "link_doctype": link_doctype, "link_name": link_name},
                    doctype=doctype, name=name, link_doctype=link_doctype,
                    link_name=link_name)

    def record_updated(self, doctype: str, name: str, before: dict) -> None:
        """An EXISTING record's fields changed; `before` holds what they were.

        Only the fields that were written are captured, so a revert restores
        those cells rather than overwriting anything changed since.
        """
        self.effect("record_update",
                    {"op": "restore_record", "doctype": doctype, "name": name,
                     "fields": dict(before)},
                    doctype=doctype, name=name, fields=sorted(before))

    def override_set(self, doctype: str, column: str, target: Optional[str],
                     previous: Optional[str], overrides_path: str) -> None:
        self.effect("override_set",
                    {"op": "restore_override", "doctype": doctype, "column": column,
                     "previous": previous, "path": overrides_path},
                    doctype=doctype, column=column, target=target)

    def close(self, status: str = "ok") -> None:
        """Close the journal, recording the outcome.

        `status` exists so this matches `MigrationContext.close`: callers hold one
        or the other (`_effect_sink`) and must not have to branch on which, which
        is how the flat import ended up calling this with a keyword it did not
        accept. A journal that recorded no effects leaves no file behind.
        """
        if self._closed:
            return
        self._closed = True
        if self._fh is None:
            return
        self._write({"event": "run_end", "run_id": self.run_id,
                     "effects": self.count, "status": status})
        self._fh.close()
        # retention runs once the file is complete, so it is never a candidate
        self.pruned, self.retention_kept = prune_journals(self._dir, protect=self.path)

    def summary(self) -> str:
        """The operator-facing line(s): the undo path, or that there is nothing to undo.

        Retention is appended when it did something, so a capped or blocked prune
        is visible rather than silent.
        """
        if not self.created:
            return "Nothing to undo: no effects were recorded."
        line = f"Journal: {self.path}  ({self.count} revertible effect(s))"
        if self.pruned:
            line += f"  [pruned {len(self.pruned)} older journal(s)]"
        if self.retention_kept:
            line += (f"  [{len(self.retention_kept)} older journal(s) kept: their "
                     f"effects are not reverted yet]")
        return line


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
    if op == "remove_record_link":
        return (f"unlink {inv.get('link_doctype')}/{inv.get('link_name')} "
                f"from {inv.get('doctype')}/{inv.get('name')}")
    if op == "restore_record":
        fields = ", ".join(sorted(inv.get("fields") or {})) or "(no fields)"
        return f"restore {inv.get('doctype')}/{inv.get('name')} {fields}"
    if op == "correction_revoke":
        return f"revoke correction {inv.get('correction_id')} in {inv.get('path')}"
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
        if op == "remove_record_link":
            return _remove_record_link(client, inv)
        if op == "restore_record":
            fields = dict(inv.get("fields") or {})
            if not fields:
                return True, ""      # nothing was changed, nothing to restore
            client.update(inv["doctype"], inv["name"], fields)
            return True, ""
        if op == "correction_revoke":
            from .corrections import revoke

            revoke(inv.get("path") or "", inv.get("correction_id") or "")
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


def _remove_record_link(client: ERPNextClient, inv: dict) -> tuple[bool, str]:
    """Drop one Dynamic Link row from a record, keeping the others."""
    doctype, name = inv["doctype"], inv["name"]
    want = (inv.get("link_doctype"), inv.get("link_name"))
    doc = client.get(doctype, name)
    links = list(doc.get("links") or [])
    kept = [r for r in links
            if (r.get("link_doctype"), r.get("link_name")) != want]
    if len(kept) == len(links):
        return True, ""          # already unlinked (or never linked)
    client.update(doctype, name, {"links": kept})
    return True, ""


def already_reverted(data: dict) -> Optional[dict]:
    """The latest revert marker in this journal, if it covers all effects.

    Public because selecting the *next* journal to revert (`latest_run`) must
    apply exactly the same rule as reverting one — they disagreed once already.
    """
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


def _journal_state(path: Path) -> tuple[int, bool]:
    """(effect count, already reverted) for a journal file."""
    data = parse_journal(path)
    return len(data["effects"]), already_reverted(data) is not None


def prune_journals(log_dir: str | Path, keep: int = KEEP_JOURNALS,
                   protect: Optional[Path] = None) -> tuple[list[Path], list[Path]]:
    """Delete the oldest journal files beyond `keep`, safest first.

    Returns `(deleted, kept_over_cap)`. A file with un-reverted effects is never
    deleted, so the cap is best-effort: if too many carry live effects, the cap
    cannot be met and the leftovers are reported instead of silently destroyed.
    Empty and already-reverted journals are deleted oldest-first, which is what
    makes the cap hold in normal use.

    `protect` is the journal that is currently open and must never be a candidate.
    Only `journal-*.jsonl` files are considered.
    """
    d = Path(log_dir)
    guard = Path(protect).resolve() if protect else None

    def creation_key(f: Path) -> tuple[str, float]:
        """Order by the stamp in the name, not mtime.

        Reverting appends a marker, which touches mtime — ordering by it would let
        an ancient reverted journal look newest and escape retention.
        """
        m = _JOURNAL_NAME.match(f.name)
        return (m.group("stamp") if m else "", f.stat().st_mtime)

    # the open journal counts towards the cap; it is skipped when deleting, never
    # removed from the list, or the count would be short by one
    files = sorted(d.glob(f"{JOURNAL_PREFIX}*.jsonl"), key=creation_key, reverse=True)

    deleted: list[Path] = []
    kept: list[Path] = []
    for f in files[keep:]:                       # oldest first within the overflow
        if guard is not None and f.resolve() == guard:
            continue
        effects, reverted = _journal_state(f)
        if effects and not reverted:
            kept.append(f)                       # never destroy the undo path
            continue
        try:
            f.unlink()
        except OSError:
            continue
        deleted.append(f)
    return deleted, sorted(kept)


def revert_journal(client: ERPNextClient, path: str | Path,
                   apply: bool = False, force: bool = False) -> dict:
    """Replay a journal's inverses LIFO (newest effect first).

    Reverting an already-reverted journal is a no-op unless `force` is set:
    effects are inverses held at effect time, so replaying them twice would
    otherwise try to delete records that are legitimately gone.
    """
    data = parse_journal(path)
    marker = already_reverted(data)
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
