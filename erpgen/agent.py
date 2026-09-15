#!/usr/bin/env python3
"""The LLM agent — LlamaIndex AgentWorkflow driving the erpgen migration loop.

Reached as `erpgen agent ...` (see `add_agent_flags`); there is no separate
script. `run(args)` takes an already-parsed namespace so the CLI owns argv.

Reads the latest mapping analysis for a doctype, then an LLM agent resolves the
conflicts using tools (create fields, create missing records, set mapping
overrides, re-run map/import) and loops until error-severity conflicts are gone,
then imports and verifies.

Usage:
  # fresh analysis + agent run
  python3 erpgen.py agent --doctype Customer --source samples/customers.csv \
      --defaults '{"customer_group":"Commercial"}'

  # resume from an existing analysis
  python3 erpgen.py agent --analysis analysis/analysis-customer-<ts>.json

  # no LLM: build tools, show plan, exit (for wiring/debugging)
  python3 erpgen.py agent --doctor --doctype Customer --source samples/customers.csv

  # pick a provider and control the outer convergence loop
  python3 erpgen.py agent --doctype Customer --source samples/customers.csv \
      --provider deepseek --max-rounds 30

LLM provider: --provider openai|deepseek|deepinfra (auto-detected from
OPENAI_API_KEY / DEEPSEEK_API_KEY / DEEPINFRA_API_KEY). DeepSeek defaults to model
deepseek-v4-flash on https://api.deepseek.com (--api-base to override).
DeepInfra defaults to model zai-org/GLM-5.3.
Run with the project venv: .venv/bin/python erpgen.py agent ...

Global flags (--base/--run/--log-dir) live on the CLI's main parser and come
before the subcommand: `erpgen.py --run acme-01 agent --source ...`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from erpgen.client import ERPNextClient  # noqa: E402
from erpgen.infer import guess_doctype  # noqa: E402
from erpgen.mapper import MappingEngine  # noqa: E402
from erpgen.metadata import fetch_with_children  # noqa: E402
from erpgen.overrides import DEFAULT_OVERRIDES, load_overrides, set_mapping  # noqa: E402
from erpgen.customers_full import (  # noqa: E402
    detect_party_sheet,
    flat_map_for,
    flow_for_party,
    parse_flat_target,
    party_for_flow,
)
from erpgen.source import read_source  # noqa: E402
from erpgen.tools import (  # noqa: E402
    create_field,
    create_record,
    describe_doctype,
    get_record,
    list_records,
)

CLIENT: ERPNextClient = None  # set in main()
_TRANSCRIPT_CTX: dict = {}  # {round, log_event} set before each agent round


# ---------------------------------------------------------------- plumbing
def _erpgen(args: list[str], timeout: int = 300) -> tuple[int, str]:
    res = subprocess.run(
        [sys.executable, str(ROOT / "erpgen.py"), *args],
        capture_output=True, text=True, cwd=ROOT, timeout=timeout,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def latest_analysis(doctype: str, source: str = ""):
    """The analysis to work from: the worksheet, or the newest snapshot.

    Worksheet-first because it is the one document a correction is recorded in;
    the timestamped `analysis/*.json` files are output-only history. The
    worksheet is matched by the `doctype`/`source.path` it carries rather than by
    filename, because a doctype slug can be a prefix of another's (Customer vs
    Customer Group) and the same trap the snapshot lookup once had.
    """
    from erpgen.analysis import analysis_paths  # noqa: PLC0415

    ws_dir = ROOT / "worksheets"
    worksheets: list[tuple[Path, dict]] = []
    if ws_dir.exists():
        for p in ws_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if d.get("doctype") == doctype:
                worksheets.append((p, d))

    if source:
        for _p, d in worksheets:
            if (d.get("source") or {}).get("path") == source:
                return d
        return None

    if len(worksheets) == 1:
        return worksheets[0][1]
    if len(worksheets) > 1:
        print(f"ERROR: {len(worksheets)} worksheets for {doctype!r}: "
              + ", ".join(p.name for p, _ in worksheets)
              + " — pass a source to pick one.", file=sys.stderr)
        return None

    files = analysis_paths(doctype, ROOT / "analysis")
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _j(v) -> str:
    return json.dumps(v, indent=2, default=str)


def _text(value) -> str:
    """Normalize an agent result to plain text.

    llama-index may hand back a str, a ChatMessage, or an AgentOutput depending
    on how the workflow finishes — unwrap any of them safely.

    A message with no content (the model ended its turn on a tool call) yields
    "", never a role repr like "user: None".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    # newer llama-index keeps text in .blocks
    blocks = getattr(value, "blocks", None) or []
    parts = [getattr(b, "text", None) for b in blocks if getattr(b, "text", None)]
    if parts:
        return "\n".join(parts)
    if content:
        return str(content)
    if hasattr(value, "role") or hasattr(value, "blocks"):
        return ""          # content-less chat message: no text to show
    return str(value)


def _safe(value, limit: int = 1000) -> str:
    """Serialize arbitrary tool args/results for the transcript log."""
    try:
        s = json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s[:limit]


# ---------------------------------------------------------------- tools
def t_latest_analysis(doctype: str) -> str:
    a = latest_analysis(doctype)
    if a is None:
        return "No analysis found. Run map first (or use the run_map tool)."
    return _j(a)


def t_run_map(source: str, doctype: str = "", defaults: str = "{}") -> str:
    cmd = ["map", source]
    src = read_source(source)
    party = detect_party_sheet(src)
    if not party and doctype:
        cmd += ["--doctype", doctype]
    if defaults and defaults != "{}":
        cmd += ["--defaults", defaults]
    code, out = _erpgen(cmd)
    dt = doctype or (flow_for_party(party) if party else guess_doctype(src))
    fresh = latest_analysis(dt, source) if dt else None
    if fresh is None:
        return f"map failed (exit {code}):\n{out[-1500:]}"
    return f"map exit {code}. Fresh analysis:\n{_j(fresh)}"


def warning_digest(out: str, cap: int = 5) -> str:
    """Group the per-row `WARNING:` lines so the *reason* survives truncation.

    Only the tail of an import is returned to the model, but with many failing
    rows the per-row warnings — which carry the actual error — sit at the front
    and get cut off. The agent then knows "29 failed" without knowing why, and
    burns rounds guessing at it.
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("WARNING:"):
            continue
        msg = " ".join(line[len("WARNING:"):].split())
        # "row 2: Customer 'Acme' failed: <reason>" — group on <reason>, not the
        # row-specific prefix, or every row looks like a distinct problem
        head, sep, body = msg.partition(" failed: ")
        key = " ".join((body if sep else msg).split())[:220]
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, head)
    if not counts:
        return ""
    total = sum(counts.values())
    lines = [f"\n{total} row warning(s), grouped by cause (most common first):"]
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for msg, n in ranked[:cap]:
        where = f"   [first: {first_seen[msg]}]" if first_seen.get(msg) else ""
        lines.append(f"  x{n}  {msg}{where}")
    if len(ranked) > cap:
        lines.append(f"  ... +{len(ranked) - cap} more distinct warning(s)")
    if len(ranked) == 1 and total > 1:
        lines.append("  -> every failing row shares ONE reason: suspect a "
                     "schema/environment problem (e.g. a custom field whose "
                     "database column is missing), not bad source data.")
    return "\n".join(lines)


def import_failure_count(out: str) -> int:
    """Row failures reported by an import run, across its three output shapes."""
    m = re.search(r"REST upsert: created \d+, failed (\d+)", out)
    if m:
        return int(m.group(1))
    flat = [int(n) for n in re.findall(r"\|\s*failed (\d+)", out)]
    if flat:
        return sum(flat)
    m = re.search(r'"failed":\s*(\d+)', out)      # --bulk result JSON
    if m:
        return int(m.group(1))
    m = re.search(r"failed:\s*(\d+)", out)        # "Import summary" block
    if m:
        return int(m.group(1))
    # last resort: the summary was truncated away, so count the row warnings
    return sum(1 for line in out.splitlines() if line.strip().startswith("WARNING:"))


def t_run_import(source: str, doctype: str = "", apply: bool = False,
                 defaults: str = "{}") -> str:
    cmd: list = []
    run_id = _TRANSCRIPT_CTX.get("run")
    if run_id:
        # --run is a GLOBAL flag, so it must precede the subcommand. Without it
        # the import journals to its own file, and `revert <run-id>` would leave
        # every imported row behind (it cannot order across files).
        cmd += ["--run", str(run_id)]
    cmd += ["import", source]
    if doctype:
        cmd += ["--doctype", doctype]
    if defaults and defaults != "{}":
        cmd += ["--defaults", defaults]
    if apply:
        cmd.append("--apply")
    code, out = _erpgen(cmd, timeout=900)
    return f"import exit {code}:\n{out[-2000:]}{warning_digest(out)}"


def t_create_field(doctype: str, label: str, fieldtype: str = "Data") -> str:
    try:
        r = create_field(CLIENT, doctype, label, fieldtype=fieldtype)
        return _j(r)
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


def t_create_record(doctype: str, fields_json: str) -> str:
    try:
        fields = json.loads(fields_json)
        r = create_record(CLIENT, doctype, fields)
        return _j(r)
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


def t_set_mapping(doctype: str, column: str, target: str) -> str:
    try:
        flow_party = party_for_flow(doctype)
        if flow_party:
            parsed = parse_flat_target(target)
            if parsed is None:
                allowed = "|".join(sorted({k for k, _ in flat_map_for(flow_party).values()}))
                return _j({"error": f"flat target must be '<doctype>.<fieldname>' "
                                    f"with doctype in {allowed} (got {target!r})"})
            dt_key, field = parsed
            canonical = {"customer": "Customer", "supplier": "Supplier",
                         "contact": "Contact", "address": "Address"}[dt_key]
            parent, children = fetch_with_children(CLIENT, canonical)
            valid = {t.qualified for t in MappingEngine(parent, children).targets}
            if field not in valid:
                return _j({"error": f"field '{field}' is not on {canonical}; "
                                    "create_field it first"})
        path = str(ROOT / DEFAULT_OVERRIDES)
        prev = ((load_overrides(path).get(doctype) or {}).get("mappings") or {}).get(column)
        set_mapping(path, doctype, column, target)
        # journal the decision like the CLI does, so it is revertible (inverse
        # restores the previous mapping) and resolves ambiguous_mapping requirements
        from erpgen import tools as _t  # noqa: PLC0415
        if _t.ACTIVE_JOURNAL is not None:
            _t.ACTIVE_JOURNAL.override_set(doctype, column, target, prev, path)
        return _j({"saved": f"{doctype}.{column} -> {target}", "file": path,
                   "previous": prev})
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


def t_correct(source: str, doctype: str, correction_json: str) -> str:
    """Record one worksheet correction; the CLI journals it so revert revokes it."""
    try:
        corr = json.loads(correction_json)
    except json.JSONDecodeError as e:
        return _j({"error": f"correction_json is not valid JSON: {e}"})
    cmd: list = []
    run_id = _TRANSCRIPT_CTX.get("run")
    if run_id:
        # --run precedes the subcommand, as with run_import, so `revert <run-id>`
        # revokes the correction alongside the rows it caused
        cmd += ["--run", str(run_id)]
    cmd += ["correct", source, "--doctype", doctype, "--by", "agent",
            "--json", json.dumps(corr)]
    code, out = _erpgen(cmd)
    return f"correct exit {code}:\n{out[-1500:]}"


def t_describe_doctype(doctype: str) -> str:
    try:
        return _j(describe_doctype(CLIENT, doctype))
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


def t_get_record(doctype: str, name: str) -> str:
    try:
        return _j(get_record(CLIENT, doctype, name))
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


def t_list_records(doctype: str, filters_json: str = "") -> str:
    try:
        filters = json.loads(filters_json) if filters_json else None
        return _j(list_records(CLIENT, doctype, filters=filters))
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


TOOLS = [
    {"fn": t_latest_analysis, "name": "latest_analysis",
     "description": "Return the most recent mapping analysis JSON for a doctype "
                    "(conflicts, mappings, suggested custom fields)."},
    {"fn": t_run_map, "name": "run_map",
     "description": "Re-run the mapper for a source file and doctype; returns the "
                    "fresh analysis JSON. Pass defaults as JSON for required fields."},
    {"fn": t_run_import, "name": "run_import",
     "description": "Run the (idempotent) import for a source file and doctype. "
                    "Set apply=True to actually import; default is a dry run."},
    {"fn": t_create_field, "name": "create_field",
     "description": "Create a custom field (column) on a doctype. Idempotent."},
    {"fn": t_create_record, "name": "create_record",
     "description": "Create a record in any doctype. Pass fields as JSON. "
                    "Idempotent by the doctype's name field. Use describe_doctype "
                    "to learn required fields."},
    {"fn": t_set_mapping, "name": "set_mapping",
     "description": "Record a forced source-column -> target-field mapping override "
                    "for a doctype. Fixes ambiguous/missed mappings. For flat party "
                    "sheets use doctype='customers_full'|'suppliers_full' and "
                    "target='<customer|supplier|contact|address>.<fieldname>' "
                    "(e.g. customer.tax_id, supplier.tax_id)."},
    {"fn": t_correct, "name": "correct",
     "description": "Record one worksheet correction for a source sheet: set_value "
                    "(fill a blank cell), skip_row (drop a row), merge_rows (fold a "
                    "duplicate into another row), dismiss_conflict (waive a conflict "
                    "with a reason), change_key (retarget the review key). Pass the "
                    "correction object as JSON; it must name the conflict it answers "
                    "in its 'conflict' field."},
    {"fn": t_describe_doctype, "name": "describe_doctype",
     "description": "Summarize a doctype's structure (required fields, links, "
                    "child tables, fetch_from, id field)."},
    {"fn": t_get_record, "name": "get_record",
     "description": "Fetch a single record by name as JSON."},
    {"fn": t_list_records, "name": "list_records",
     "description": "List records of a doctype (optional ERPNext filters JSON)."},
]

SYSTEM_PROMPT = """You are the ERPNext migration-fix agent. You resolve mapping
conflicts found by the erpgen mapper, then import the data.

Conflict kinds and how to fix them:
- unmapped_column: create_field for the column (label = column name), then re-run map.
- ambiguous_mapping: decide the intended target and force it with set_mapping, then re-run map.
- link_value_conflict: create the missing option records with create_record
  (use describe_doctype to learn the required fields and the name field), then re-run map.
- fetch_from: the target field is read-only (populated from another doc); you
  cannot write it directly. Note it and move on.
- required_missing: pass defaults to run_map/run_import.
- duplicate_row: the same key appears on more than one row. Resolve the
  conflicting cells in one row, drop the duplicate, or point --id-column at a
  column that is unique per entity. You cannot invent a value.
- missing_value: the cell is empty in the source sheet. Record the value as a
  worksheet correction, or use defaults when a constant is legitimate. You cannot
  invent the value.
- possible_duplicate_row: WARNING only, never blocks. Two rows may be one entity
  spelled two ways. Review the pairs: merge them, unify the spelling with a
  value_map, or dismiss the conflict if they are genuinely separate. Do not try to
  make this kind disappear before importing.

Record every correction with the `correct` tool (never edit files by hand). A
correction must name the conflict it answers in its `conflict` field (the key is
"<kind>:<source or field>[:<target>]"); for duplicate_row, merge_rows/skip_row
close it, and for a conflict you have decided to accept, dismiss_conflict with a
reason closes it.

Flat party sheets (doctype='customers_full' or 'suppliers_full'; ONE file with
a party + Contact + Address): every conflict is an out-of-contract column and is
error-severity (blocking). Resolve each via its suggested_action:
- resolution=extend_contract -> set_mapping('<flow>', column,
  '<doctype>.<target>'), e.g. set_mapping('customers_full', 'Tax ID', 'customer.tax_id').
- resolution=create_custom_field -> first create_field(doctype, label, fieldtype)
  (or run the suggested create_command), then set_mapping('<flow>', column,
  '<doctype>.<fieldname>') using the suggested fieldname.
The flow name and suggested commands are already filled in for you — use them
verbatim. Then run_map again and confirm zero conflicts remain before importing.
Contacts/Addresses shared between party types are linked automatically on import.

Per-iteration workflow:
1. Read the analysis from the user message or latest_analysis.
2. Resolve every error-severity conflict: create fields/records, set mappings,
   or record a worksheet correction with `correct` (set_value for a blank,
   skip_row/merge_rows for a duplicate, dismiss_conflict to waive one with a
   reason). A corrected conflict turns "corrected"/"waived" on the next map.
3. run_map again and confirm the error-severity conflicts decreased. Repeat until zero.
4. run_import with apply=True.
5. Verify with get_record / list_records if useful, then give a final summary.

Rules:
- Never import while error-severity conflicts remain open or stale.
- Imports are idempotent: re-running is safe and skips existing records.
- Never modify source files; record decisions via set_mapping / create_field / correct.
- Tools return JSON; reason over it before acting."""  # noqa: E501


# ---------------------------------------------------------------- llm
# Live progress, streamed to stdout. A single round can take minutes and make
# dozens of remote calls; without this the CLI user watches a blank screen and
# has to guess whether it is working. `--quiet` silences it.
_LIVE: dict = {"enabled": True, "indent": "  "}


def _live(message: str) -> None:
    if _LIVE["enabled"]:
        print(message, flush=True)


def _brief(value: Any, limit: int = 100) -> str:
    """One-line, length-capped rendering for live output."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _brief_args(args, kwargs) -> str:
    if kwargs:
        parts = [f"{k}={_brief(v, 45)}" for k, v in kwargs.items()]
    else:
        parts = [_brief(a, 45) for a in args]
    return ", ".join(parts)


def _thinking(response) -> str:
    """The model's own words for this step — its visible reasoning."""
    msg = getattr(response, "message", None)
    return _brief(getattr(msg, "content", "") or "", 170)


def _requested_tools(response) -> list:
    """Names of the tools the model asked for in this response.

    llama-index exposes these on the response, which delegates to the message;
    fall back to the message directly for response shapes that only set it there.
    """
    calls = getattr(response, "tool_calls", None)
    if not calls:
        calls = getattr(getattr(response, "message", None), "tool_calls", None)
    names: list = []
    for call in (calls or []):
        name = getattr(call, "tool_name", None)
        if not name:
            tool = getattr(call, "tool", None)
            name = (getattr(getattr(tool, "metadata", None), "name", None)
                    or getattr(tool, "name", None))
        if name:
            names.append(str(name))
    return names


def _live_llm_request(messages) -> None:
    if _LIVE["enabled"]:
        _live(f"{_LIVE['indent']}· llm #{_TRANSCRIPT_CTX.get('llm_calls', 0)} → "
              f"{len(messages)} msg(s)")


def _live_llm_response(response, started: float) -> None:
    """Show what one remote call cost, and what it decided to do next."""
    if not _LIVE["enabled"]:
        return
    bits = [f"{_elapsed_ms(started)}ms"]
    usage = llm_usage(response)
    if usage.get("total_tokens") is not None:
        bits.append(f"{usage['total_tokens']:,} tok")
    wanted = _requested_tools(response)
    if wanted:
        bits.append("wants " + ", ".join(wanted))
    _live(f"{_LIVE['indent']}· llm #{_TRANSCRIPT_CTX.get('llm_calls', 0)} ← "
          + " · ".join(bits))
    thought = _thinking(response)
    if thought:
        _live(f"{_LIVE['indent']}    “{thought}”")


def _log_llm_event(event: str, **fields) -> None:
    """Record an LLM transport event on the active transcript (if any).

    Also counts requests, so the run can report how many remote calls it cost.
    """
    if event == "llm_request":
        _TRANSCRIPT_CTX["llm_calls"] = _TRANSCRIPT_CTX.get("llm_calls", 0) + 1
        fields.setdefault("call_no", _TRANSCRIPT_CTX["llm_calls"])
    log = _TRANSCRIPT_CTX.get("log_event")
    if log:
        log(event=event, round=_TRANSCRIPT_CTX.get("round"), **fields)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def llm_usage(response) -> dict:
    """Token usage from a ChatResponse, when the provider reports it.

    Deliberately tolerant: `raw` may be an OpenAI object, a plain dict, or
    absent entirely, and a missing usage block must never break a run.
    """
    raw = getattr(response, "raw", None)
    usage = getattr(raw, "usage", None)
    if usage is None and isinstance(raw, dict):
        usage = raw.get("usage")
    if usage is None:
        return {}

    def _get(key):
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _get(key)
        if value is not None:
            out[key] = value
    return out


def instrumented_llm(base_cls):
    """Wrap an LLM class so every remote call lands in the run transcript.

    llama-index does NOT emit callback events for `OpenAILike` (the
    `@llm_chat_callback()` decorator is only applied in `custom.py` and
    `structured_llm.py`), so this instruments the two entry points
    `FunctionAgent` actually funnels through: `achat_with_tools` -> `achat`,
    and `astream_chat_with_tools` -> `astream_chat`.

    Emits `llm_request` / `llm_response` / `llm_failure` with the round, elapsed
    time and token usage. Message *content* is never logged — only counts.
    """

    class _Instrumented(base_cls):
        async def achat(self, *args, **kwargs):
            messages = kwargs.get("messages") or (args[0] if args else None) or []
            started = time.monotonic()
            _log_llm_event(event="llm_request", method="achat",
                           model=str(getattr(self, "model", "") or ""),
                           messages=len(messages))
            _live_llm_request(messages)
            try:
                response = await super().achat(*args, **kwargs)
            except BaseException as e:
                _log_llm_event(event="llm_failure", method="achat",
                               duration_ms=_elapsed_ms(started),
                               error=f"{type(e).__name__}: {e}")
                _live(f"{_LIVE['indent']}· llm failed after "
                      f"{_elapsed_ms(started)}ms: {type(e).__name__}")
                raise
            _log_llm_event(event="llm_response", method="achat",
                           duration_ms=_elapsed_ms(started),
                           **llm_usage(response))
            _live_llm_response(response, started)
            return response

        async def astream_chat(self, *args, **kwargs):
            messages = kwargs.get("messages") or (args[0] if args else None) or []
            started = time.monotonic()
            _log_llm_event(event="llm_request", method="astream_chat",
                           model=str(getattr(self, "model", "") or ""),
                           messages=len(messages))
            _live_llm_request(messages)
            try:
                inner = await super().astream_chat(*args, **kwargs)
            except BaseException as e:
                # fails before a generator exists — still a remote call attempt
                _log_llm_event(event="llm_failure", method="astream_chat",
                               duration_ms=_elapsed_ms(started),
                               error=f"{type(e).__name__}: {e}")
                _live(f"{_LIVE['indent']}· llm failed after "
                      f"{_elapsed_ms(started)}ms: {type(e).__name__}")
                raise

            async def _logged():
                last = None
                try:
                    async for chunk in inner:
                        last = chunk
                        yield chunk
                except BaseException as e:
                    _log_llm_event(event="llm_failure", method="astream_chat",
                                   duration_ms=_elapsed_ms(started),
                                   error=f"{type(e).__name__}: {e}")
                    _live(f"{_LIVE['indent']}· llm stream failed after "
                          f"{_elapsed_ms(started)}ms: {type(e).__name__}")
                    raise
                # a streaming call is only complete once fully drained
                _log_llm_event(event="llm_response", method="astream_chat",
                               duration_ms=_elapsed_ms(started),
                               streamed=True, **llm_usage(last))
                _live_llm_response(last, started)

            return _logged()

    _Instrumented.__name__ = f"Instrumented{base_cls.__name__}"
    return _Instrumented


#: hard bound on one LLM request, so a hung endpoint surfaces as a timeout
#: error instead of an open connection the operator has to Ctrl-C out of
LLM_TIMEOUT = 120


def _deepseek_llm(model: str, api_base: str):
    """OpenAI-compatible client for DeepSeek.

    Uses llama-index's OpenAILike (not the OpenAI class, whose metadata
    property validates model names against OpenAI's registry and rejects
    DeepSeek model ids). is_function_calling_model=True is required for the
    FunctionAgent tool loop.
    """
    from llama_index.llms.openai_like import OpenAILike

    return instrumented_llm(OpenAILike)(
        model=model or "deepseek-v4-flash",
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        api_base=api_base or "https://api.deepseek.com",
        is_chat_model=True,
        is_function_calling_model=True,
        # fail fast and loud: no silent SDK retries, and a hard read bound so a
        # hung request surfaces as APITimeoutError instead of an open connection
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


def _deepinfra_llm(model: str, api_base: str):
    """OpenAI-compatible client for DeepInfra (GLM and friends)."""
    from llama_index.llms.openai_like import OpenAILike

    return instrumented_llm(OpenAILike)(
        model=model or "zai-org/GLM-5.3",
        api_key=os.environ.get("DEEPINFRA_API_KEY"),
        api_base=api_base or "https://api.deepinfra.com/v1/openai",
        is_chat_model=True,
        is_function_calling_model=True,
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


def llm_preflight(api_base: str, api_key: str, *, timeout: int = 10,
                  resolve=None, open_url=None) -> tuple[bool, str]:
    """Probe the LLM endpoint so a network problem is reported clearly.

    The OpenAI SDK wraps *every* transport failure — DNS, TLS verification,
    a dead proxy, connection refused — in a bare `APIConnectionError` with no
    hint about which. This resolves the host, does a real request, and reports
    the HTTP status: 401/403 means the endpoint is reachable, so the problem is
    the key rather than the network.

    `resolve`/`open_url` are injection seams so this can be unit-tested offline.
    """
    import socket
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    resolve = resolve or socket.getaddrinfo
    open_url = open_url or urllib.request.urlopen

    url = api_base if "://" in api_base else f"https://{api_base}"
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    proxies = {k: v for k, v in os.environ.items()
               if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}

    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
    except Exception as e:  # noqa: BLE001 — DNS failure is the whole point
        return False, (f"DNS lookup failed for {host}: {type(e).__name__}: {e}\n"
                       f"  proxies in env: {proxies or 'none'}")

    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with open_url(req, timeout=timeout) as resp:
            return True, f"{url} reachable (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return True, (f"{url} reachable (HTTP {e.code}) — network is fine, "
                          "check the API key")
        if e.code == 404:
            # DNS, TCP and TLS all succeeded — an API root routinely has no
            # handler, so blaming --api-base here is misleading noise.
            return True, (f"{url} reachable (HTTP 404 at the root — normal for "
                          "many API hosts)")
        return True, (f"{url} reachable (HTTP {e.code}) — if calls also fail, "
                      "check --api-base")
    except Exception as e:  # noqa: BLE001
        return False, (f"cannot reach {url}: {type(e).__name__}: {e}\n"
                       f"  resolved {host} -> {', '.join(ips)}\n"
                       f"  proxies in env: {proxies or 'none'}")


def describe_llm_error(exc: BaseException, api_base: str, model: str = "",
                       provider: str = "") -> str:
    """Turn any LLM failure into an actionable help block.

    Classifies by HTTP status first, then by class name, because the OpenAI SDK
    raises the *same* `APIConnectionError` for DNS failure, TLS verification
    errors, a dead proxy and a refused connection. Always prints the endpoint and
    model actually in use, then concrete next steps.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    raw = " ".join(str(exc).split())[:300]
    low = f"{name} {raw}".lower()
    curl = "curl -sS -m 5 -o /dev/null -w '%{http_code}\\n' " + api_base + "/"

    if status == 401 or "authenticationerror" in low:
        cause = "The API key is missing, wrong, or revoked (HTTP 401)."
        steps = ["export DEEPSEEK_API_KEY=... then re-run",
                 "Or use --provider openai with OPENAI_API_KEY."]
    elif status == 403 or "permissiondenied" in low:
        cause = "The key is valid but not allowed to use this model (HTTP 403)."
        steps = ["Check the key's project/scope, or change --model."]
    elif status == 404 or "notfounderror" in low:
        cause = "Endpoint or model not found (HTTP 404)."
        steps = [f"Model in use: {model or '(provider default)'} — check the exact id.",
                 "For DeepSeek use: --api-base https://api.deepseek.com"]
    elif status == 429 or "ratelimit" in low:
        cause = "Rate limited or out of quota (HTTP 429)."
        steps = ["Retry shortly; lower --max-rounds; check the account balance."]
    elif status == 400 or "badrequesterror" in low:
        cause = ("The request was rejected (HTTP 400) — usually context length, "
                 "or a tool schema the model refuses.")
        steps = ["Try one smaller sheet, or a model with a larger context window."]
    elif isinstance(status, int) and 500 <= status < 600:
        cause = f"The provider failed server-side (HTTP {status})."
        steps = ["Retry; if it persists, try --provider openai."]
    elif any(k in low for k in ("connection", "connect", "timeout", "ssl",
                                "proxy", "unreachable", "getaddrinfo")):
        cause = ("Network/transport failure — DNS, TLS verification, a dead proxy "
                 "or a firewall. The SDK reports all of these identically.")
        steps = ["env | grep -iE 'proxy'    # a stale HTTPS_PROXY is the usual cause",
                 curl,
                 "401 from curl = network works (so the key is the problem);",
                 "no response at all = blocked by DNS/VPN/firewall.",
                 "Or pass --api-base <url>, or --provider openai."]
    else:
        cause = "Unexpected LLM failure."
        steps = ["Re-run with AGENT_DEBUG=1 to see the full traceback."]

    out = [f"LLM call failed — {name}: {raw}", "",
           f"  endpoint : {api_base}",
           f"  model    : {model or '(provider default)'}"]
    if provider:
        out.append(f"  provider : {provider}")
    out += ["", f"  {cause}", "", "  Next steps:"]
    out += [f"    {s}" for s in steps]
    out += ["", "  No LLM needed (builds every analysis offline):",
            "    DOCTOR=1 scripts/run-all-agentic.sh"]
    return "\n".join(out)


def _is_iteration_exhausted(exc: BaseException) -> bool:
    """True when the workflow stopped because its internal step budget ran out."""
    name = type(exc).__name__
    if "WorkflowRuntimeError" in name or "MaxIterations" in name:
        return True
    return "max iterations" in str(exc).lower()


def _is_llm_error(exc: BaseException) -> bool:
    """True for OpenAI/httpx API failures, as opposed to a bug in our own code."""
    if type(exc).__module__.split(".")[0] in ("openai", "httpx", "httpcore"):
        return True
    name = type(exc).__name__.lower()
    return any(k in name for k in ("apierror", "connection", "timeout", "ratelimit",
                                   "authentication", "permissiondenied", "notfound"))


def _openai_llm(model: str):
    """OpenAI LLM, instrumented so its calls are logged like DeepSeek's."""
    from llama_index.llms.openai import OpenAI

    return instrumented_llm(OpenAI)(model=model or "gpt-4o-mini",
                                    timeout=LLM_TIMEOUT, max_retries=0)


def get_llm(provider: str, model: str, api_base: str = ""):
    if provider == "openai":
        return _openai_llm(model)
    if provider == "deepseek":
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise SystemExit(
                "DEEPSEEK_API_KEY is not set. export DEEPSEEK_API_KEY=... "
                "(or pass --provider openai)"
            )
        return _deepseek_llm(model, api_base)
    if provider == "deepinfra":
        if not os.environ.get("DEEPINFRA_API_KEY"):
            raise SystemExit(
                "DEEPINFRA_API_KEY is not set. export DEEPINFRA_API_KEY=... "
                "(or pass --provider openai)"
            )
        return _deepinfra_llm(model, api_base)
    if os.environ.get("OPENAI_API_KEY"):
        return _openai_llm(model)
    if os.environ.get("DEEPSEEK_API_KEY"):
        return _deepseek_llm(model, api_base)
    if os.environ.get("DEEPINFRA_API_KEY"):
        return _deepinfra_llm(model, api_base)
    raise SystemExit(
        "No LLM provider configured. Set OPENAI_API_KEY, DEEPSEEK_API_KEY "
        "or DEEPINFRA_API_KEY and choose --provider openai|deepseek|deepinfra."
    )


def _wrap_tool(fn, name):
    """Wrap a tool function so every call is logged to the run transcript.

    functools.wraps preserves the original signature, so FunctionTool still
    builds the correct JSON schema for the model.
    """
    import functools

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        ctx = _TRANSCRIPT_CTX
        log = ctx.get("log_event")
        rnd = ctx.get("round")
        call_args = kwargs if kwargs else args
        if log:
            log(event="tool_call", round=rnd, name=name,
                kwargs=_safe(call_args))
        # greppable marker: `grep ToolUse:` lists every tool the agent ran
        _live(f"{_LIVE['indent']}  ToolUse:{name} {_brief_args(args, kwargs)}")
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            # a raising tool must be visible immediately, not just in the log
            if log:
                log(event="tool_result", round=rnd, tool=name,
                    args=_safe(call_args), result_tail=f"raised {type(e).__name__}: {e}")
            _live(f"{_LIVE['indent']}    ToolResult:{name} ✗ "
                  f"{type(e).__name__}: {_brief(e, 90)}")
            raise
        if log:
            log(event="tool_result", round=rnd, tool=name,
                args=_safe(call_args),
                result_tail=_text(result)[-600:])
        text = _text(result)
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        _live(f"{_LIVE['indent']}    ToolResult:{name} ✓ {_brief(first, 100)}")
        return result

    return wrapped


# ---------------------------------------------------------------- workflow
def build_workflow(llm):
    from llama_index.core.agent.workflow import AgentWorkflow, FunctionAgent
    from llama_index.core.tools import FunctionTool

    tools = [
        FunctionTool.from_defaults(fn=_wrap_tool(t["fn"], t["name"]),
                                   name=t["name"], description=t["description"])
        for t in TOOLS
    ]
    agent = FunctionAgent(
        name="migration_agent",
        description="Resolves ERPNext migration conflicts and imports data.",
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        llm=llm,
        verbose=True,
        timeout=900,
        # non-streaming: the OpenAI-compatible hosts this targets answer a plain
        # `achat` in a few seconds, but their SSE stream can sit open without
        # ever sending a chunk, hanging the run until the client cancels it
        streaming=False,
    )
    return AgentWorkflow(agents=[agent], root_agent="migration_agent", timeout=900)


# ---------------------------------------------------------------- main
def cmd_doctor(args) -> int:
    doctype, flat = resolve_doctype(args)
    where = "inferred from headers" if doctype and not args.doctype else "from --doctype"
    print(f"Agent doctor (no LLM call) — doctype={doctype or '(unresolved)'} "
          f"({where}{', flat party sheet' if flat else ''})")
    a = latest_analysis(doctype) if doctype else None
    if a is None:
        print("  no analysis yet; run: python3 erpgen.py map <source>")
    else:
        print(f"  latest analysis: {a['source_rows']} rows, "
              f"{len(a['conflicts'])} conflicts, "
              f"{len(a['suggested_custom_fields'])} suggested fields")
        for c in a["conflicts"]:
            print(f"    [{c['severity']:<7}] {c['kind']}")
    print(f"\nTools ({len(TOOLS)}):")
    for t in TOOLS:
        print(f"  - {t['name']}: {t['description'][:80]}")
    print(f"\nOverrides file: {ROOT / DEFAULT_OVERRIDES}")
    return 0


async def _run_agent_round(workflow, user_msg: str, max_iterations: int = 0) -> str:
    """Run one agent round.

    Tool calls and results are logged by the tool wrappers via _TRANSCRIPT_CTX
    (set by run_agent before each round). Here we await the workflow — the
    WorkflowHandler is awaitable but NOT async-iterable in llama-index 0.14 —
    and return the final response text.

    `max_iterations` caps the workflow's INTERNAL step budget (llama-index
    defaults to 20, which a conflict-heavy sheet can exhaust while still making
    progress). 0 means "use the library default".
    """
    kwargs = {"max_iterations": max_iterations} if max_iterations else {}
    result = await workflow.run(user_msg=user_msg, **kwargs)
    text = _text(getattr(result, "response", None)) or _text(getattr(result, "raw", None))
    if not text.strip():
        calls = getattr(result, "tool_calls", None) or []
        text = (f"(no text response; the agent ended its turn with "
                f"{len(calls)} tool call(s) — see the transcript)") if calls \
            else "(no text response)"
    return text


def resolve_doctype(args) -> tuple[Optional[str], bool]:
    """Resolve the target doctype for a run.

    Returns `(doctype_or_flow_name, is_flat_party_sheet)`. An explicit `--doctype`
    always wins; otherwise it is **inferred from the source headers**.

    `--doctype` deliberately has no argparse default: a default would shadow the
    inference entirely and silently map every sheet against that one doctype
    (e.g. Item columns analysed as Customer fields).
    """
    if not args.source:
        return args.doctype, False
    src = read_source(args.source)
    party = detect_party_sheet(src)
    if party:
        return flow_for_party(party), True
    return args.doctype or guess_doctype(src), False


@dataclass
class Transcript:
    """The agent's monotonic audit log: run_start, tool calls, rounds, run_end."""

    logs_dir: Path
    path: Path
    _fh: Any

    @classmethod
    def open(cls, doctype: str) -> "Transcript":
        logs_dir = ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
        path = logs_dir / f"agent-{doctype.lower().replace(' ', '-')}-{stamp}.jsonl"
        return cls(logs_dir, path, path.open("w", encoding="utf-8"))

    def log(self, **entry) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


@dataclass
class RunOutcome:
    """How the convergence loop finished."""

    converged: bool = False
    response: str = ""
    exit_code: int = 0  # non-zero => bailed out (LLM failure / step budget)


def _llm_base(args) -> str:
    return args.api_base or "https://api.deepseek.com"


def _preflight_llm(args) -> bool:
    """Probe the endpoint once, so a network problem is not a mid-loop traceback."""
    if args.provider == "openai":
        return True
    base = _llm_base(args)
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    ok, detail = llm_preflight(base, key)
    if not ok:
        # the probe knows *why*; the shared help block knows the next steps
        print(f"ERROR: LLM endpoint unreachable — {detail}", file=sys.stderr)
        print(describe_llm_error(ConnectionError(detail), base,
                                 args.model or "", args.provider), file=sys.stderr)
        return False
    print(f"LLM preflight: {detail}")
    return True


def _load_analysis(args, doctype: Optional[str], flat: bool) -> Optional[dict]:
    """The analysis to work from: --analysis, or freshly mapped, or the newest."""
    if args.analysis:
        return json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    if args.source:
        cmd = ["map", args.source]
        if doctype and not flat:
            cmd += ["--doctype", doctype]
        if args.defaults:
            cmd += ["--defaults", args.defaults]
        _erpgen(cmd)
        analysis = latest_analysis(doctype, args.source)
        if analysis is None:
            print("ERROR: map produced no analysis", file=sys.stderr)
        return analysis
    analysis = latest_analysis(doctype) if doctype else None
    if analysis is None:
        print("ERROR: no analysis found. Pass --source or --analysis.", file=sys.stderr)
    return analysis


def _error_conflicts(analysis: dict) -> list:
    """The conflicts that still stop an import.

    Status, not severity: a `corrected`/`waived`/`resolved` conflict no longer
    needs the agent's attention, or the loop would stall re-fixing what the
    worksheet already answered.
    """
    return [c for c in analysis["conflicts"]
            if c["severity"] == "error"
            and c.get("status", "open") in ("open", "stale")]


#: conflict kinds the deterministic cleaning stage owns. The mapping agent is
#: gated until these are corrected/waived, so the LLM never spends a token on
#: "which rows are duplicates" — the detectors already answered that for free.
DATA_QUALITY_KINDS = ("duplicate_row", "missing_value")

#: exit code when the mapping agent is refused until data quality is clean
EXIT_DATA_QUALITY = 5


def _data_quality_blockers(analysis: dict) -> list:
    """Error-severity cleaning conflicts that still block the mapping agent.

    `possible_duplicate_row` is warning-only and never blocks, so it is not a
    gate. Data quality is resolved deterministically — worksheet corrections via
    `erpgen.py correct` — not by the LLM.
    """
    return [c for c in analysis["conflicts"]
            if c["severity"] == "error"
            and c.get("status", "open") in ("open", "stale")
            and c["kind"] in DATA_QUALITY_KINDS]


def _stall_fingerprint(errs: list) -> frozenset:
    """Identity of the outstanding error conflicts, for stall detection.

    Keyed on each conflict's own identity rather than the count: a count can stay
    flat while entirely different conflicts come and go, which is progress.
    """
    return frozenset(
        (c.get("kind"), c.get("source"), c.get("target") or c.get("field"),
         c.get("doctype"))
        for c in errs
    )


def _advance_stall(fingerprint: frozenset, prev_fingerprint: Optional[frozenset],
                   stalled: int) -> int:
    """Consecutive rounds whose outstanding error conflicts are unchanged.

    Resets to 0 the moment anything changes — a different conflict, or one fewer
    — because that is progress even if the count merely stayed level.
    """
    if prev_fingerprint is not None and fingerprint and fingerprint == prev_fingerprint:
        return stalled + 1
    return 0


def _describe_conflict(c: dict) -> str:
    where = c.get("source") or c.get("field") or "?"
    tail = f" -> {c['doctype']}" if c.get("doctype") else (
        f" -> {c['target']}" if c.get("target") else "")
    return f"{c.get('kind')}: {where}{tail}"


def _report_stall(errs: list, rounds: int, transcript_path, args) -> None:
    """Explain a stall, because 'still failing' alone gives the user nothing."""
    print(f"\n=== AGENT STALLED — the same {len(errs)} error conflict(s) survived "
          f"{rounds} round(s) unchanged ===", file=sys.stderr)
    for c in errs:
        print(f"    [error] {_describe_conflict(c)}", file=sys.stderr)
    print("\n  The agent is not making progress, so more rounds will not help.\n"
          "  Usual causes — a conflict the available tools cannot satisfy:\n"
          "    * the conflict names a doctype that does not exist, or names a\n"
          "      FIELD where a doctype is expected\n"
          "    * the target field is read-only / computed and cannot be written\n"
          "    * the value has to be created somewhere the agent cannot reach\n"
          f"\n  Failing tool calls: {transcript_path}"
          "\n\n  Next steps:"
          "\n    --max-stall-rounds 0      disable this check and run to --max-rounds"
          f"\n    --max-rounds {args.max_rounds * 2}        allow more rounds anyway"
          "\n    or resolve it by hand (set-mapping / createfield / create-record)"
          "\n    and re-run the same command — it resumes from the analysis.",
          file=sys.stderr)


def _open_journal(args, doctype: str, source: str, conflicts: list,
                  logs_dir: Path, log_event):
    """Join the unified run context when --run is given, else a per-run journal.

    Either way every effect the agent applies is journaled, so the whole run is
    revertible with one command.
    """
    from erpgen import tools as erpgen_tools  # noqa: PLC0415

    if getattr(args, "run", None):
        from erpgen.context import MigrationContext  # noqa: PLC0415
        journal = MigrationContext(args.run, logs_dir, source=source or "agent",
                                   base_url=args.base, doctypes=[doctype],
                                   command="agent")
        seeded = journal.add_requirements(conflicts)
        log_event(event="run_context_open", path=str(journal.path), requirements=seeded)
        print(f"Run context: {journal.path}  ({seeded} requirement(s) recorded)")
    else:
        from erpgen.journal import MigrationJournal  # noqa: PLC0415
        journal = MigrationJournal(logs_dir, doctype=doctype, source=source or "agent",
                                   base_url=args.base)
        log_event(event="journal_open", path=str(journal.path))
    erpgen_tools.ACTIVE_JOURNAL = journal
    return journal


def _round_message(round_no: int, doctype: str, source: str, analysis: dict,
                   pre_import: str = "") -> str:
    """Round 1 gets the whole analysis; later rounds get what is still failing."""
    if round_no == 1:
        failed_note = (
            "\n\nA deterministic import was already run and these rows FAILED — "
            f"diagnose the cause, fix it, then import again:\n{pre_import}"
            if pre_import else ""
        )
        return (
            f"Resolve the migration conflicts for doctype '{doctype}' and import the data.\n"
            f"Source file: {source}\n"
            f"Base URL: {analysis.get('base_url', '')}\n\n"
            f"Current analysis:\n{json.dumps(analysis, indent=2, default=str)}"
            f"{failed_note}"
        )
    return (
        f"Round {round_no}. These error-severity conflicts REMAIN after your last "
        f"round:\n{json.dumps(_error_conflicts(analysis), indent=2, default=str)}\n\n"
        "Fix them (create_field / create_record / set_mapping), then run_map to "
        "confirm they are gone. Import only once no error conflicts remain."
    )


def _report_round_error(exc: BaseException, args, round_no: int,
                        transcript: Transcript) -> Optional[int]:
    """Classify a failed round: an exit code to bail with, or None to re-raise."""
    if _is_iteration_exhausted(exc):
        print(f"\nERROR: the agent exhausted its internal step budget "
              f"({args.max_iterations or 20} iterations) in round {round_no}.\n"
              "  This usually means the sheet has many conflicts, or the "
              "model is looping on a tool.\n"
              "  Next steps:\n"
              f"    raise the budget:  --max-iterations "
              f"{max((args.max_iterations or 20) * 2, 40)}\n"
              "    or run this sheet alone and inspect the transcript:\n"
              f"      {transcript.path}\n"
              "    or use the offline path: DOCTOR=1 scripts/run-all-agentic.sh",
              file=sys.stderr)
        transcript.log(event="iteration_exhausted", round=round_no,
                       max_iterations=args.max_iterations,
                       error=f"{type(exc).__name__}: {exc}")
        return 3
    if not _is_llm_error(exc):
        return None
    print("\n" + describe_llm_error(exc, _llm_base(args), args.model or "",
                                    args.provider), file=sys.stderr)
    transcript.log(event="llm_error", round=round_no,
                   error=f"{type(exc).__name__}: {exc}")
    return 2


async def _run_rounds(args, workflow, transcript: Transcript, analysis: dict,
                      doctype: str, source: str, pre_import: str = "") -> RunOutcome:
    """Loop until no error-severity conflicts remain, or the round cap is hit.

    Each round re-reads the analysis artifact rather than trusting the model's
    own claim of success.
    """
    outcome = RunOutcome()
    prev_error_count: Optional[int] = None
    prev_fingerprint: Optional[frozenset] = None
    stalled = 0

    for round_no in range(1, args.max_rounds + 1):
        errs = _error_conflicts(analysis)
        print(f"\n=== Round {round_no}/{args.max_rounds} — {len(errs)} error conflict(s), "
              f"{len(analysis['conflicts'])} total ===")
        for c in errs:
            print(f"    [error] {c['kind']}: {c.get('source') or c.get('field')}")

        transcript.log(event="round_start", round=round_no, errors_before=len(errs),
                       conflicts_total=len(analysis["conflicts"]))
        _TRANSCRIPT_CTX.update({"round": round_no, "log_event": transcript.log,
                                "run": getattr(args, "run", None)})
        try:
            outcome.response = await _run_agent_round(
                workflow, _round_message(round_no, doctype, source, analysis,
                                         pre_import if round_no == 1 else ""),
                args.max_iterations)
        except Exception as e:  # noqa: BLE001 — classify, don't dump a traceback
            code = _report_round_error(e, args, round_no, transcript)
            if code is None:
                raise
            outcome.exit_code = code
            return outcome

        # programmatic verification: re-read the newest analysis artifact
        fresh = latest_analysis(doctype)
        if fresh is not None:
            analysis = fresh
        after_errs = _error_conflicts(analysis)

        # stall detection: same conflicts, unchanged, round after round
        fingerprint = _stall_fingerprint(after_errs)
        stalled = _advance_stall(fingerprint, prev_fingerprint, stalled)
        prev_fingerprint = fingerprint

        note = ("  [no decrease vs previous round]"
                if prev_error_count is not None and len(after_errs) >= prev_error_count
                else "")
        if stalled:
            note += f"  [stalled {stalled}]"
        print(f"--- Round {round_no}: {len(after_errs)} error(s) remain{note}")
        transcript.log(event="round_end", round=round_no, errors_before=len(errs),
                       errors_after=len(after_errs), stalled=stalled,
                       conflicts_total=len(analysis["conflicts"]),
                       response=outcome.response[-2000:])

        if not after_errs:
            print("\n=== AGENT CONVERGED (no error-severity conflicts) ===\n")
            print(outcome.response)
            outcome.converged = True
            return outcome

        # escape hatch: stop burning rounds on a conflict that cannot be resolved
        if args.max_stall_rounds and stalled >= args.max_stall_rounds:
            transcript.log(event="stalled", round=round_no, stalled_rounds=stalled,
                           conflicts=[_describe_conflict(c) for c in after_errs])
            _report_stall(after_errs, stalled, transcript.path, args)
            outcome.exit_code = 4
            return outcome

        prev_error_count = len(after_errs)

    print(f"\nReached max rounds ({args.max_rounds}) with unresolved error conflicts.")
    print(outcome.response)
    return outcome


def _report_run_end(journal, transcript: Transcript) -> None:
    if hasattr(journal, "pending_requirements"):  # unified run context
        pending = journal.pending_requirements()
        print(f"\nRun context: {journal.path}  ({journal.effects} effect(s), "
              f"{len(pending)} requirement(s) still pending)")
        for r in pending:
            print(f"    PENDING  {r.get('kind')}: {r.get('detail')}")
        journal.close(status="ok")
        print(f"  revert the whole run: python3 erpgen.py revert {journal.run_id} --apply")
    else:
        journal.close()
        print(f"\n{journal.summary()}")
        print(f"  revert with: python3 erpgen.py revert {journal.path}")
    calls = _TRANSCRIPT_CTX.get("llm_calls", 0)
    print(f"\nAgent transcript (LLM calls + tool calls + responses): {transcript.path}")
    print(f"  {calls} remote LLM call(s) logged — grep 'llm_request' / 'llm_failure'")


async def run_agent(args) -> int:
    doctype, flat = resolve_doctype(args)

    analysis = _load_analysis(args, doctype, flat)
    if analysis is None:
        return 2

    print(f"Starting agent for {analysis['doctype']} — {len(analysis['conflicts'])} conflicts")
    for c in analysis["conflicts"]:
        print(f"  [{c['severity']:<7}] {c['kind']:<22} "
              f"{c.get('source') or c.get('field')}")

    doctype = analysis["doctype"]
    src = analysis.get("source")
    source = (src.get("path") if isinstance(src, dict) else src) \
        or args.source or "unknown"

    # Data quality gates the mapping agent: duplicate keys and empty required
    # cells are found by deterministic detectors, so they must be resolved with
    # worksheet corrections (no LLM) before the model is allowed to spend a token
    # on mapping decisions.
    dq = _data_quality_blockers(analysis)
    if dq:
        print(f"\n=== {len(dq)} data-quality conflict(s) must be fixed before "
              "the mapping agent can run ===")
        for c in dq:
            print(f"    [{c['kind']}] {c.get('source') or c.get('field')}")
        print("  Fix them deterministically (no LLM needed) with worksheet "
              "corrections, e.g.:")
        print(f"    python3 erpgen.py correct {source} --doctype {doctype} "
              "--json '{\"action\": \"set_value\", \"at\": {\"row\": N}, ...}'")
        print("  duplicate_row -> skip_row / merge_rows; missing_value -> "
              "set_value; or dismiss_conflict to waive one with a reason.")
        print("  Re-run once they are corrected/waived/resolved.")
        return EXIT_DATA_QUALITY

    # With no conflicts there is nothing for the model to decide, so run the
    # import deterministically and only wake the agent if rows actually failed.
    pre_import = ""
    if args.source and not _error_conflicts(analysis) and not args.always_llm:
        cmd = (["--run", args.run] if getattr(args, "run", None) else [])
        cmd += ["import", args.source, "--apply"]
        code, out = _erpgen(cmd, timeout=900)
        failed = import_failure_count(out)
        print(f"\nNo conflicts — deterministic import (exit {code}): "
              f"{failed} row(s) failed")
        if not failed:
            print(out.strip().splitlines()[-1] if out.strip() else "")
            return 0
        pre_import = warning_digest(out) or out[-1500:]
        print(f"  {failed} row(s) failed — engaging the agent to investigate.")

    llm = get_llm(args.provider, args.model, args.api_base)
    if not _preflight_llm(args):
        return 2

    workflow = build_workflow(llm)

    transcript = Transcript.open(doctype)
    transcript.log(event="run_start", doctype=doctype, source=source, base=args.base,
                   provider=args.provider, model=args.model or "(provider default)",
                   max_rounds=args.max_rounds)
    journal = _open_journal(args, doctype, source, analysis["conflicts"],
                            transcript.logs_dir, transcript.log)

    outcome = await _run_rounds(args, workflow, transcript, analysis, doctype, source,
                                pre_import)
    if outcome.exit_code == 0:
        transcript.log(event="run_end", max_rounds=args.max_rounds,
                       resolved=outcome.converged,
                       llm_calls=_TRANSCRIPT_CTX.get("llm_calls", 0))
    transcript.close()

    from erpgen import tools as erpgen_tools  # noqa: PLC0415
    erpgen_tools.ACTIVE_JOURNAL = None
    if outcome.exit_code:
        return outcome.exit_code

    _report_run_end(journal, transcript)
    return 0




def add_agent_flags(ap: argparse.ArgumentParser) -> None:
    """Agent-specific flags, added to the `erpgen agent` subparser.

    Global flags (--base/--user/--password/--log-dir/--run) are added separately
    by the CLI, so `erpgen agent --run X` and `erpgen --run X agent` both work.
    """
    # no default: a default would shadow header inference (see resolve_doctype)
    ap.add_argument("--doctype", default=None,
                    help="target doctype (inferred from --source headers if omitted)")
    ap.add_argument("--source", help="source CSV/XLSX to analyze (runs map first)")
    ap.add_argument("--analysis", help="path to an existing analysis JSON")
    ap.add_argument("--defaults", help='JSON defaults for map/import, e.g. \'{"customer_group":"Commercial"}\'')
    ap.add_argument("--provider", default="auto",
                    choices=["auto", "openai", "deepseek", "deepinfra"])
    ap.add_argument("--model", help="LLM model (provider default if omitted)")
    ap.add_argument("--api-base", help="OpenAI-compatible base URL "
                                       "(DeepSeek default: https://api.deepseek.com)")
    ap.add_argument("--max-iterations", type=int, default=50,
                    help="internal tool-call budget per round (llama-index "
                         "default: 20; raise it for conflict-heavy sheets)")
    ap.add_argument("--max-rounds", type=int, default=20,
                    help="outer convergence loop cap (default: 20)")
    ap.add_argument("--max-stall-rounds", type=int, default=2,
                    help="give up after this many consecutive rounds with the "
                         "SAME conflicts unchanged (default: 2; 0 disables)")
    ap.add_argument("--always-llm", action="store_true",
                    help="always engage the agent, even when the analysis has no "
                         "conflicts (default: import deterministically first and "
                         "only involve the agent if rows fail)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the live per-call progress stream on stdout "
                         "(round headers still print; the transcript is "
                         "unaffected)")
    ap.add_argument("--doctor", action="store_true",
                    help="show tools + analysis without calling an LLM")


def build_parser() -> argparse.ArgumentParser:
    """Standalone parser (tests, and `--help` off the CLI)."""
    ap = argparse.ArgumentParser(prog="erpgen agent", description=__doc__)
    add_agent_flags(ap)
    ap.add_argument("--base", default="http://localhost:8082")
    ap.add_argument("--user", default="Administrator")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--run", metavar="RUN_ID", help="migration run id")
    return ap


def run(args) -> int:
    """Entry point for the `erpgen agent` subcommand (argv parsed by the CLI)."""
    _LIVE["enabled"] = not getattr(args, "quiet", False)

    if args.doctor:
        # doctor only inspects files + tools — no ERPNext connection needed
        return cmd_doctor(args)

    global CLIENT
    CLIENT = ERPNextClient(args.base, username=args.user, password=args.password)

    base = args.api_base or "https://api.deepseek.com"
    try:
        return asyncio.run(run_agent(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing was left half-imported: every write is "
              "idempotent and journaled — inspect with "
              f"'python3 erpgen.py status {args.run or '<run-id>'}'.",
              file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 — last-resort friendly failure
        if _is_llm_error(e):
            print("\n" + describe_llm_error(e, base, args.model or "", args.provider),
                  file=sys.stderr)
            return 2
        print(f"\nERROR: agent failed — {type(e).__name__}: {e}", file=sys.stderr)
        if os.environ.get("AGENT_DEBUG"):
            raise
        print("  Re-run with AGENT_DEBUG=1 for the full traceback.", file=sys.stderr)
        return 1
