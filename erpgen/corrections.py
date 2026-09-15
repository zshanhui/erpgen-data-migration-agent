"""Worksheet corrections: the five verbs, verified and applied before load.

The worksheet (`worksheets/<doctype>-<source>.json`) is the only place a data
correction lives, because the detectors always read the **raw** source: a
correction changes the *payload*, never what gets detected. That asymmetry is the
whole design, and it is why a correction must be *verified* rather than confirmed
by re-detection — see `docs/gen/worksheet_schema.md`.

`prepare` is pure: it never mutates the source table it is handed, and returns a
corrected copy plus a verdict per correction. The only I/O here is reading the
worksheet file.

Verification split: the row-effect verbs (`set_value`, `skip_row`,
`merge_rows`) and `change_key` are verified here, because everything they need is
in hand — the sheet's rows, columns and key column. `dismiss_conflict` is only
shape-checked here; whether the conflict it names still exists is decided in
`statuses`, which is the only function that sees the fresh detection.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .source import SourceTable

#: worksheets live beside analysis/ and logs/
WORKSHEET_DIR = "worksheets"

ACTIONS = ("set_value", "skip_row", "merge_rows", "dismiss_conflict", "change_key")

#: a decision without a reason is not reviewable
NEEDS_REASON = ("skip_row", "dismiss_conflict", "change_key")

#: a waiver has to name what it waives
NEEDS_CONFLICT = ("dismiss_conflict",)

#: verbs that change the rows
ROW_ACTIONS = ("set_value", "skip_row", "merge_rows")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def slug(doctype: str, source_path: str) -> str:
    """Worksheet filename stem for a `(doctype, source)` pair."""
    return f"{_slug(doctype)}-{_slug(Path(source_path).stem)}"


def worksheet_path(doctype: str, source_path: str,
                   worksheet_dir: str | Path = WORKSHEET_DIR) -> Path:
    return Path(worksheet_dir) / f"{slug(doctype, source_path)}.json"


def file_sha256(path: str | Path) -> str:
    """Hash of the source file, the guard for position-keyed corrections."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def load_worksheet(doctype: str, source_path: str,
                   worksheet_dir: str | Path = WORKSHEET_DIR) -> dict:
    """The worksheet for `(doctype, source)`, or `{}` when there is none.

    Malformed JSON raises rather than being ignored: silently dropping a
    worksheet would import a sheet whose corrections the operator believes are in
    force.
    """
    return read_worksheet(worksheet_path(doctype, source_path, worksheet_dir))


def read_worksheet(path: str | Path) -> dict:
    """Read a worksheet by path; `{}` when absent, an error when malformed."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"worksheet {p} is unreadable: {e}") from e
    return data if isinstance(data, dict) else {}


def _ref_key(ref: Any) -> tuple:
    """A hashable form of a row reference, for idempotency comparison."""
    if isinstance(ref, str):
        return ("value", ref)
    if isinstance(ref, dict) and set(ref) == {"row"}:
        return ("row", ref["row"])
    return ("other", json.dumps(ref, sort_keys=True, default=str))


def _identity(corr: dict) -> tuple:
    """The meaning of a correction, ignoring the tool-added fields.

    Two corrections are the same decision when their action, row references,
    target cells and answered conflict agree — regardless of `id`, `created_at`,
    `note`, `reason` or the key column the tool recorded.
    """
    action = corr.get("action")
    parts: list[Any] = [action]
    if action in ("set_value", "skip_row"):
        parts.append(_ref_key(corr.get("at")))
    elif action == "merge_rows":
        parts.append(_ref_key(corr.get("keep")))
        parts.append(tuple(sorted(_ref_key(r) for r in (corr.get("drop") or []))))
        parts.append(tuple(sorted((corr.get("field_overrides") or {}).items())))
    if action == "set_value":
        parts.append(corr.get("column"))
        parts.append(corr.get("value"))
    if action == "change_key":
        parts.append(corr.get("column"))
    parts.append(corr.get("conflict"))
    return tuple(parts)


def add_correction(path: str | Path, correction: dict,
                   created_by: str = "human") -> tuple[dict, bool]:
    """Append one correction to the worksheet at `path`, unless already recorded.

    Authoring is idempotent like every other mutation in the tool: an identical
    **active** correction is returned unchanged (a revoked one is history, so
    re-authoring after a revocation is a fresh decision and does append).
    Returns `(correction, created)`.

    Validates against the sheet's columns (from `column_profiles`) so a typo is
    rejected at authoring time, before it is journaled and before it can confuse
    the gate on a later run. The caller must have a worksheet (run `map` first).
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"no worksheet at {p}; run `map` first")
    data = read_worksheet(p)
    headers = [prof.get("header", "") for prof in (data.get("column_profiles") or [])]
    corr = dict(correction)
    corr.setdefault("created_at", dt.datetime.now(dt.timezone.utc)
                    .isoformat(timespec="seconds"))
    corr.setdefault("created_by", created_by)
    why = _validate(corr, headers)
    if why:
        raise ValueError(why)
    for existing in data.get("corrections") or []:
        if not existing.get("revoked_at") and _identity(existing) == _identity(corr):
            return existing, False
    ids = {c.get("id") for c in (data.get("corrections") or [])}
    n = 0
    while True:
        n += 1
        if f"c{n}" not in ids:
            corr["id"] = f"c{n}"
            break
    data.setdefault("corrections", []).append(corr)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return corr, True


def revoke(path: str | Path, correction_id: str, reason: str = "reverted") -> bool:
    """Revoke a correction in place; immutable and auditable, like the rest."""
    p = Path(path)
    data = read_worksheet(p)
    for c in data.get("corrections") or []:
        if c.get("id") == correction_id and not c.get("revoked_at"):
            c["revoked_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            c["revoked_reason"] = reason
            p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return True
    return False


def active(worksheet: dict) -> list[dict]:
    """Corrections that are still in force (a revoked one no longer applies)."""
    return [c for c in (worksheet.get("corrections") or [])
            if isinstance(c, dict) and not c.get("revoked_at")]


def merged_corrections(previous: list[dict], prepared: "Prepared") -> list[dict]:
    """The worksheet's corrections, refreshed from `prepared`.

    Revoked corrections are kept for the audit trail; every active one is
    replaced by its tool-filled copy (`id`, `key_column`, `from`, …). Alignment
    is by position — `prepared.corrections` holds exactly the active corrections
    of `previous`, in the same order — because a hand-written correction has no
    `id` to match on before the tool assigns one.
    """
    active_cs = iter(prepared.corrections)
    out: list[dict] = []
    for c in previous or []:
        if c.get("revoked_at"):
            out.append(c)
        else:
            out.append(next(active_cs, c))
    return out


def conflict_key(conflict: dict) -> str:
    """`<kind>:<source or field>[:<target>]` — what a correction answers.

    The target is appended only when there is one, so a correction written before
    a column was mapped keeps naming the same conflict afterwards.
    """
    head = str(conflict.get("source") or conflict.get("field") or "")
    key = f"{conflict.get('kind')}:{head}"
    if conflict.get("target"):
        key = f"{key}:{conflict['target']}"
    return key


@dataclass
class Prepared:
    """One sheet's corrections, resolved and applied."""

    #: the rows to build payloads from (the original table when nothing applied)
    source: SourceTable
    #: the corrections as loaded, with the tool-added fields filled in
    corrections: list[dict] = field(default_factory=list)
    #: correction id -> {"applicable": bool, "why": str, "invalid": bool}
    verdicts: dict[str, dict] = field(default_factory=dict)
    changed: bool = False
    #: the key column corrections and detection agree on, after `change_key`
    key_column: str = ""
    key_changed: bool = False

    def applied(self) -> list[str]:
        return [cid for cid, v in self.verdicts.items() if v["applicable"]]

    def inert(self) -> list[str]:
        return [cid for cid, v in self.verdicts.items() if not v["applicable"]]

    def invalid(self) -> list[str]:
        """Corrections rejected at load — a mistake in the worksheet itself."""
        return [cid for cid, v in self.verdicts.items() if v["invalid"]]


def _refs(correction: dict) -> list:
    """Every row reference a correction makes."""
    action = correction.get("action")
    if action in ("set_value", "skip_row"):
        return [correction.get("at")]
    if action == "merge_rows":
        return [correction.get("keep")] + list(correction.get("drop") or [])
    return []


def _cell(row: list, col: int) -> Any:
    return row[col] if 0 <= col < len(row) else ""


def _put(row: list, col: int, value: Any) -> None:
    while len(row) <= col:
        row.append("")
    row[col] = value


def _validate(c: dict, headers: list[str]) -> Optional[str]:
    """Reasons a correction cannot be used at all, or None."""
    action = c.get("action")
    if action not in ACTIONS:
        return f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}"
    if action in NEEDS_REASON and not _text(c.get("reason")):
        return f"{action} needs a reason"
    if action in NEEDS_CONFLICT and not _text(c.get("conflict")):
        return f"{action} must name the conflict it answers"
    for ref in _refs(c):
        shape = _ref_shape(ref)
        if shape:
            return shape
    if action == "set_value":
        if "value" not in c:
            return "set_value needs a value"
        if not _text(c.get("column")):
            return "set_value needs a column"
        if c["column"] not in headers:
            return f"{c['column']!r} is not a column in this sheet"
    elif action == "merge_rows":
        if "keep" not in c:
            return "merge_rows needs 'keep'"
        drops = c.get("drop")
        if not isinstance(drops, list):
            return "merge_rows needs 'drop' (a list, possibly empty)"
        if any(d == c["keep"] for d in drops):
            return "merge_rows names the kept row in drop too"
        unknown = [col for col in (c.get("field_overrides") or {})
                   if col not in headers]
        if unknown:
            return f"field_overrides column(s) not in this sheet: {', '.join(unknown)}"
    elif action == "change_key":
        if not _text(c.get("column")):
            return "change_key needs a column"
        if c["column"] not in headers:
            return f"{c['column']!r} is not a column in this sheet"
    return None


def _ref_shape(ref: Any) -> Optional[str]:
    """Reject a row reference that is not a key value or `{"row": N}`.

    A bare number is refused on purpose: a numeric key value and a row number
    would otherwise be the same JSON.
    """
    if ref is None:
        return "the correction has no row reference"
    if isinstance(ref, str):
        return None
    if isinstance(ref, dict) and set(ref) == {"row"}:
        n = ref["row"]
        if isinstance(n, int) and not isinstance(n, bool):
            return None
        return "the row number must be an integer"
    return 'a row reference must be a key value (a string) or {"row": N}'


def _resolve(ref: Any, rows: list[list], headers: list[str], key_column: str,
             numbers: list[int], written_sha: str, sha256: str) -> tuple:
    """`(index, why)` — the row a reference names, or why it names none."""
    if isinstance(ref, dict):
        n = ref["row"]
        if written_sha and sha256 and written_sha != sha256:
            return None, ("the source sheet changed since this correction was "
                          "written, so its row number may point elsewhere now")
        try:
            return numbers.index(n), None
        except ValueError:
            return None, f"the sheet has no row {n}"
    if not key_column:
        return None, "no key column is recorded for this correction"
    if key_column not in headers:
        return None, f"the key column {key_column!r} is not in this sheet"
    col = headers.index(key_column)
    hits = [i for i in range(len(rows)) if _text(_cell(rows[i], col)) == ref]
    if not hits:
        return None, f"no row has {key_column} = {ref!r}"
    if len(hits) > 1:
        return None, (f"{len(hits)} rows have {key_column} = {ref!r}, so the "
                      f'reference is ambiguous; name one with {{"row": N}}')
    return hits[0], None


def _apply(c: dict, rows: list[list], headers: list[str], key_column: str,
           numbers: list[int], dropped: set, sha256: str) -> Optional[str]:
    """Apply one correction to `rows`; return None if it applied, else why not."""
    action = c["action"]
    written = _text(c.get("source_sha256"))

    def resolve(ref):
        # a value reference resolves against the column it was written against,
        # not the (possibly changed) current key
        return _resolve(ref, rows, headers, c.get("key_column") or key_column,
                        numbers, written, sha256)

    if action == "set_value":
        idx, why = resolve(c["at"])
        if why:
            return why
        if idx in dropped:
            return "the row was dropped by an earlier correction"
        col = headers.index(c["column"])
        now = _text(_cell(rows[idx], col))
        if "from" not in c:
            # first sighting: the value the correction is replacing is what the
            # sheet holds now, and `from` is what later runs compare against
            c["from"] = now
        elif _text(c["from"]) != now:
            return (f"the cell now holds {now!r}, not {_text(c['from'])!r} as it "
                    f"did when the correction was written")
        _put(rows[idx], col, c["value"])
        return None

    if action == "skip_row":
        idx, why = resolve(c["at"])
        if why:
            return why
        if idx in dropped:
            # the row is already gone (a sibling skip_row / merge_rows dropped
            # it): the intent is satisfied, so this is an idempotent no-op —
            # NOT a contradiction. Multiple missing_value conflicts on one row
            # each propose a skip_row, and only the first may actually drop it.
            return None
        dropped.add(idx)
        return None

    if action == "merge_rows":
        keep, why = resolve(c["keep"])
        if why:
            return why
        if keep in dropped:
            return "the kept row was dropped by an earlier correction"
        drops: list[int] = []
        for ref in c.get("drop") or []:
            idx, why = resolve(ref)
            if why:
                return why
            if idx == keep:
                return "the kept row is also in drop"
            drops.append(idx)
        for col, value in (c.get("field_overrides") or {}).items():
            _put(rows[keep], headers.index(col), value)
        dropped.update(drops)
        return None

    if action == "change_key":
        return None  # its effect is the key column `prepare` resolves up front

    return None  # dismiss_conflict: verified in statuses, against detection


def prepare(source: SourceTable, worksheet: dict, *,
            sha256: str = "") -> Prepared:
    """Resolve a worksheet's corrections against the sheet they were written for.

    Returns a corrected copy of `source` for the payload builders; the detectors
    keep reading the original. The correction dicts are filled in as they are
    seen (`id`, `created_at`, `key_column`, `from`, `source_sha256`) — that is how
    those fields reach the worksheet when it is written back.
    """
    corrections = [dict(c) for c in active(worksheet)]
    base = _text((worksheet.get("source") or {}).get("key_column"))
    if not corrections:
        return Prepared(source=source, key_column=base, key_changed=False)

    # A value-keyed correction resolves against the key in effect *where it sits
    # in the file*: a `change_key` retargets what follows it, never what precedes
    # it, so an existing correction stays applicable.
    current = base
    for c in corrections:
        if c.get("action") == "change_key" and _text(c.get("column")) in source.headers:
            current = _text(c["column"])
        for ref in _refs(c):
            if isinstance(ref, str):
                c.setdefault("key_column", current)
    key_column = current
    key_changed = current != base

    rows = [list(r) for r in source.rows]
    numbers = [source.row_number(i) for i in range(source.n_rows)]
    dropped: set[int] = set()
    verdicts: dict[str, dict] = {}
    seen_ids: set[str] = set()

    n = 0
    for c in corrections:
        c.setdefault("created_at", dt.datetime.now(dt.timezone.utc)
                     .isoformat(timespec="seconds"))
        c.setdefault("created_by", "human")
        if sha256:
            c.setdefault("source_sha256", sha256)
        if not c.get("id"):
            n += 1
            while f"c{n}" in seen_ids:
                n += 1
            c["id"] = f"c{n}"
        cid = str(c["id"])
        if cid in seen_ids:
            verdicts[cid] = {"applicable": False, "invalid": True,
                             "why": f"the id {cid} is used twice"}
            continue
        seen_ids.add(cid)

        invalid = _validate(c, source.headers)
        if invalid:
            verdicts[cid] = {"applicable": False, "invalid": True,
                             "why": invalid}
            continue
        why = _apply(c, rows, source.headers, key_column, numbers, dropped, sha256)
        verdicts[cid] = {"applicable": why is None, "invalid": False,
                         "why": why or ""}

    kept = [i for i in range(len(rows)) if i not in dropped]
    out = SourceTable(
        name=source.name,
        headers=list(source.headers),
        rows=[rows[i] for i in kept],
        profiles=source.profiles,
        # source line numbers survive a skip: they are what the worksheet, the
        # logs and {"row": N} all refer to
        row_numbers=[numbers[i] for i in kept],
    )
    changed = bool(dropped) or any(
        c["action"] in ROW_ACTIONS and verdicts[str(c["id"])]["applicable"]
        for c in corrections)
    return Prepared(source=out, corrections=corrections, verdicts=verdicts,
                    changed=changed, key_column=key_column,
                    key_changed=key_changed)


def statuses(conflicts: list[dict], prepared: Prepared, *,
             sha256: str = "", previous: Optional[list[dict]] = None,
             previous_hash: str = "") -> list[dict]:
    """Merge fresh conflicts with the previous worksheet's, assigning status.

    The detectors read the raw source, so a corrected conflict is re-detected
    every run; status is derived from re-detection plus whether each correction
    could be applied — never from "the conflict disappeared". Conflicts the
    detectors no longer report are carried forward from `previous` as `resolved`
    (or `stale`, when the sheet's hash changed under a correction whose
    bookkeeping the edit invalidated).

    `history` is append-only and carried forward; `status` and `resolution` are
    always recomputed and never read back.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def ev(event: str, **extra) -> dict:
        return {"at": now, "event": event, **extra}

    prev_by_key = {conflict_key(c): c for c in (previous or [])}
    fresh_keys: set[str] = set()
    out: list[dict] = []

    for c in conflicts:
        key = conflict_key(c)
        fresh_keys.add(key)
        prev = prev_by_key.get(key)
        prev_status = (prev or {}).get("status")
        history = list((prev or {}).get("history") or [])
        if not prev:
            history.append(ev("detected"))

        answers = [a for a in prepared.corrections
                   if str(a.get("conflict")) == key]
        status, resolution = _fresh_status(answers, prepared)

        # record a transition, never a repeat: "detected" already says a
        # first-sight open conflict, so only a change from a previous status
        # (or a non-open outcome) earns another event
        if status == "open":
            if prev_status and prev_status != "open":
                history.append(ev(status))
        elif status != prev_status:
            history.append(ev(status))
        out.append({**c, "status": status, "resolution": resolution,
                    "history": history})

    # conflicts the detectors no longer report: resolutions persist by design
    for c in (previous or []):
        key = conflict_key(c)
        if key in fresh_keys:
            continue
        answers = [a for a in prepared.corrections
                   if str(a.get("conflict")) == key]
        hash_changed = bool(previous_hash and sha256
                            and previous_hash != sha256)
        if answers and hash_changed:
            status = "stale"
        else:
            status = "resolved"
        history = list(c.get("history") or [])
        if status != c.get("status"):
            history.append(ev(status))
        out.append({**c, "status": status, "resolution": None,
                    "history": history})
    return out


def _fresh_status(answers: list[dict], prepared: Prepared) -> tuple:
    """`(status, resolution)` for a conflict that is still detected.

    A waiver wins over everything: it is a deliberate decision to accept the
    conflict. Otherwise an inert answering correction is `stale` (blocks), and
    only when every answering correction applied is the conflict `corrected`.
    """
    for a in reversed(answers):
        if a.get("action") == "dismiss_conflict":
            return "waived", {"by": "dismiss_conflict", "reason": a.get("reason"),
                              "id": a.get("id")}
    if not answers:
        return "open", None
    inert = [a for a in answers
             if not prepared.verdicts.get(str(a.get("id")), {}).get("applicable", False)]
    if inert:
        return "stale", None
    return "corrected", {"by": "correction", "ids": [a.get("id") for a in answers]}

