"""Unified migration context — Cordis-style spatiotemporal composability.

One file per migration: `logs/run-<run-id>.jsonl`. It holds BOTH dimensions:

  * requirements (coeffects) — what the migration *needs* (mapper conflicts:
    missing lookup records, unmapped columns, unmet required fields)
  * effects (journal)        — what it *changed*, each with the inverse that
    undoes it (journal-compatible lines, so `revert` works unchanged)

plus a per-run config delta. Because the context is keyed by `--run <id>` rather
than by command, every command in one migration appends to the same file — so
`erpgen.py revert --run <id>` undoes the whole migration (records + custom
fields + lookup records + overrides) in one LIFO replay.

Requirements are satisfied *reactively*: when an effect creates the thing a
pending requirement asked for, the requirement is marked satisfied and the
satisfying effect is referenced.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

RUN_PREFIX = "run-"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip()).strip("-").lower()


def resolve_run(run: str, log_dir: str | Path = "logs") -> Path:
    """Accept a run id or an explicit path."""
    p = Path(run)
    if p.is_file():
        return p
    candidate = Path(log_dir) / f"{RUN_PREFIX}{_slug(run)}.jsonl"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"no run file for {run!r} (looked for {candidate})")


def _read_entries(path: Path) -> list[dict]:
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries



def _identity_of(entry: dict) -> tuple:
    """Stable identity for a requirement, so re-analysis cannot duplicate it."""
    return (entry.get("kind") or "", entry.get("source") or "",
            entry.get("target") or "", entry.get("doctype") or "",
            entry.get("field") or "")


def _norm(value: Optional[str]) -> str:
    """Loose comparison key: source header labels vs derived fieldnames."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


class MigrationContext:
    """Append-only context for one migration run (shared across commands)."""

    def __init__(self, run_id: str, log_dir: str | Path = "logs",
                 source: str = "", base_url: str = "",
                 doctypes: Optional[Iterable[str]] = None,
                 command: str = "") -> None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.path = d / f"{RUN_PREFIX}{_slug(run_id)}.jsonl"
        self._pending: dict[str, dict] = {}
        self._covered: dict[str, set] = {}   # partial progress on multi-value requirements
        self._ident_pending: dict[tuple, str] = {}
        self._ident_done: dict[tuple, str] = {}
        self._req_seq = 0
        self.effects = 0
        self._replaying = False
        self._closed = False

        # rehydrate from the existing run file: one migration spans many
        # commands, so effect sequence numbers, unmet requirements and partial
        # progress must carry over (otherwise every command restarts at seq 1)
        effects: list[dict] = []
        if self.path.exists():
            for e in _read_entries(self.path):
                if e.get("event") == "effect":
                    self.effects += 1
                    effects.append(e)
                elif e.get("event") == "requirement":
                    self._pending[e.get("id")] = e
                    self._ident_pending[_identity_of(e)] = e.get("id")
                    digits = "".join(ch for ch in str(e.get("id", ""))[1:].split("-")[0]
                                     if ch.isdigit())
                    self._req_seq = max(self._req_seq, int(digits) if digits else 0)
                elif e.get("event") == "requirement_satisfied":
                    req = self._pending.pop(e.get("id"), None)
                    if req is not None:
                        ident = _identity_of(req)
                        self._ident_pending.pop(ident, None)
                        self._ident_done[ident] = e.get("id")
            # replay past effects to rebuild in-memory progress without
            # re-logging satisfaction events that are already on disk
            self._replaying = True
            for e in effects:
                info = {k: v for k, v in e.items()
                        if k not in ("event", "kind", "seq", "inverse", "ts")}
                self._react(e.get("kind", ""), seq=e.get("seq", 0), **info)
            self._replaying = False

        self._fh = self.path.open("a", encoding="utf-8")  # append: many commands, one run
        docs = sorted(doctypes or [])
        self._log(event="run_start", run_id=run_id, command=command,
                  source=source or None, base_url=base_url or None,
                  doctypes=docs, doctype=docs[0] if len(docs) == 1 else None)

    # ------------------------------------------------------------ plumbing
    def _log(self, **entry) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def close(self, status: str = "ok") -> None:
        if self._closed:                      # closing twice must not raise
            return
        self._closed = True
        self._log(event="run_end", run_id=self.run_id, status=status,
                  effects=self.effects, pending=len(self._pending))
        self._fh.close()

    def summary(self) -> str:
        """Operator-facing line, matching `MigrationJournal.summary` so the two
        sinks are interchangeable at the call sites that print one."""
        line = f"Run context: {self.path}  ({self.effects} revertible effect(s))"
        if self.effects == 0:
            line += " — nothing to undo"
        pending = len(self._pending)
        if pending:
            line += f", {pending} requirement(s) still pending"
        return line

    # ------------------------------------------------------------ coeffects
    def add_requirements(self, conflicts: list[dict]) -> int:
        """Record mapper conflicts as requirements. Returns how many were added."""
        added = 0
        for c in conflicts:
            ident = _identity_of(c)
            if ident in self._ident_pending:
                continue                      # already an open requirement: not a new one
            reopened = ident in self._ident_done
            self._req_seq += 1
            rid = f"r{self._req_seq}-{c.get('kind', 'conflict')}"
            detail = c.get("detail") or c.get("kind", "")
            # note: severity/detail/kind are passed explicitly below, so they
            # must not also appear in **extra (duplicate kwarg)
            extra = {k: c[k] for k in ("source", "target", "doctype", "field",
                                       "missing_values")
                     if k in c}
            self._pending[rid] = {"id": rid, "kind": c.get("kind"), "detail": detail,
                                  **extra}
            self._ident_pending[ident] = rid
            self._log(event="requirement", id=rid, kind=c.get("kind"),
                      severity=c.get("severity"), detail=detail,
                      reopened=reopened or None, **extra)
            added += 1
        return added

    def satisfy(self, rid: str, by_effect: int, log: bool = True,
                via: str = "effect") -> None:
        req = self._pending.pop(rid, None)
        if req is not None:
            ident = _identity_of(req)
            self._ident_pending.pop(ident, None)
            self._ident_done[ident] = rid
            self._covered.pop(rid, None)
            if log and not self._replaying:
                self._log(event="requirement_satisfied", id=rid, by_effect=by_effect,
                          via=via, kind=req.get("kind"))

    def satisfy_conflict(self, conflict: dict, via: str = "correction") -> bool:
        """Close the requirement a conflict describes, if one is pending.

        A worksheet correction is not an effect — nothing on the site changed —
        so it closes the requirement by identity with `via: "correction"`, which
        keeps the run log reading *requirement → satisfied by correction* rather
        than pretending a fix landed.
        """
        ident = _identity_of(conflict)
        rid = self._ident_pending.get(ident)
        if rid is None:
            return False
        self.satisfy(rid, by_effect=0, via=via)
        return True

    def note_condition(self, kind: str, **info) -> None:
        """Record that a requirement's condition already holds in the world.

        Unlike an effect this creates nothing and journals nothing to undo: the
        target (custom field, record, link value) already existed, so the
        requirement is met without the run owning an inverse for it.
        """
        self._react(kind, seq=0, condition=True, **info)

    def _react(self, kind: str, **info) -> None:
        """Mark requirements satisfied by an effect that just happened (best-effort).

        This is the coeffect side of the context: requirements recorded by an
        earlier command are re-checked against every effect, so a fix applied in
        one command (or process) closes the requirement it addresses.
        """
        seq = info.get("seq", 0)
        for rid, req in list(self._pending.items()):
            rkind = req.get("kind")

            via = "condition" if info.get("condition") else "effect"

            if rkind == "unmapped_column" and kind == "custom_field_create":
                src = _norm(req.get("source"))
                cands = {_norm(info.get("label")), _norm(info.get("fieldname"))}
                if src and src in cands:
                    self.satisfy(rid, seq, via=via)

            elif rkind == "link_value_conflict" and kind == "record_create":
                missing = req.get("missing_values") or []
                if req.get("doctype") and req["doctype"] == info.get("doctype") \
                        and info.get("name") in missing:
                    # partial progress: only close once every missing value exists
                    done = self._covered.setdefault(rid, set())
                    done.add(info["name"])
                    if set(missing) <= done:
                        self._covered.pop(rid, None)
                        self.satisfy(rid, seq, via=via)

            elif rkind == "ambiguous_mapping" and kind == "override_set":
                # a mapping override keyed on the ambiguous source column is the
                # decision that resolves it (target may be a rival field or None)
                if req.get("source") and req["source"] == info.get("column"):
                    rd, ed = req.get("doctype"), info.get("doctype")
                    if rd in (None, "") or rd == ed:
                        self.satisfy(rid, seq, via=via)

    def pending_requirements(self) -> list[dict]:
        return list(self._pending.values())

    # ------------------------------------------------------------ effects
    def effect(self, kind: str, inverse: dict, **detail) -> int:
        self.effects += 1
        self._log(event="effect", seq=self.effects, kind=kind, inverse=inverse, **detail)
        self._react(kind, seq=self.effects, **detail)
        return self.effects

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

    def override_set(self, doctype: str, column: str, target: Optional[str],
                     previous: Optional[str], overrides_path: str) -> None:
        self.effect("override_set",
                    {"op": "restore_override", "doctype": doctype, "column": column,
                     "previous": previous, "path": overrides_path},
                    doctype=doctype, column=column, target=target)

    # ------------------------------------------------------------ config delta
    def config_delta(self, path: str, doctype: str, changes: dict) -> None:
        self._log(event="config_delta", path=path, doctype=doctype, changes=changes)


# ---------------------------------------------------------------- views
def load_run(run: str, log_dir: str | Path = "logs") -> dict:
    """Read a run file into {path, run_id, requirements, effects, config_delta}."""
    path = resolve_run(run, log_dir)
    run_id: Optional[str] = None
    requirements: dict[str, dict] = {}
    effects: list[dict] = []
    config: list[dict] = []
    for e in _read_entries(path):
        ev = e.get("event")
        if ev == "run_start":
            run_id = e.get("run_id") or run_id
        elif ev == "requirement":
            requirements[e.get("id")] = {**e, "satisfied_by": None, "via": None}
        elif ev == "requirement_satisfied":
            if e.get("id") in requirements:
                requirements[e["id"]]["satisfied_by"] = e.get("by_effect")
                requirements[e["id"]]["via"] = e.get("via") or "effect"
        elif ev == "effect":
            effects.append(e)
        elif ev == "config_delta":
            config.append(e)
    return {
        "path": str(path),
        "run_id": run_id,
        "requirements": list(requirements.values()),
        "effects": effects,
        "config_delta": config,
    }


def latest_run(doctype: str, log_dir: str | Path = "logs",
               require_effects: bool = False) -> Optional[Path]:
    """Newest undoable migration log for this doctype.

    Considers both unified run files (run-<id>.jsonl) and per-command journals
    (journal-<doctype>-<ts>.jsonl). "Newest" means newest by modification time —
    NOT filename, since run ids are arbitrary and do not sort chronologically.
    Files already reverted are skipped so --latest keeps pointing at real work.
    """
    d = Path(log_dir)
    slug = _slug(doctype)
    files = list(d.glob(f"{RUN_PREFIX}*.jsonl")) + list(d.glob("journal-*.jsonl"))

    def mentions(f: Path) -> bool:
        try:
            return doctype in f.read_text(encoding="utf-8") or slug in _slug(f.stem)
        except OSError:
            return False

    cands = [f for f in files if mentions(f)]
    cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    def reverted(f: Path) -> bool:
        try:
            from .journal import already_reverted, parse_journal  # noqa: PLC0415

            return already_reverted(parse_journal(f)) is not None
        except Exception:  # noqa: BLE001
            return False

    live = [f for f in cands if not reverted(f)]
    if not live:
        return cands[0] if cands else None

    if require_effects:
        # `revert --latest` wants something to undo: a run that changed nothing
        # (or a context holding only requirements) would otherwise shadow an
        # older log that does have effects
        for f in live:
            try:
                from .journal import parse_journal  # noqa: PLC0415

                if parse_journal(f)["effects"]:
                    return f
            except Exception:  # noqa: BLE001
                continue
    return live[0]
