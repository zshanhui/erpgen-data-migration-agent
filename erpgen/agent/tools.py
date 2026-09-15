"""The agent's tools: the eleven operations it may call.

Each `t_*` function is a thin, journalled wrapper, returning text the model can
read. `TOOLS` is the registry `build_workflow` turns into llama-index
`FunctionTool`s.

They shell out to `erpgen.py` rather than calling the library directly so every
action lands in the run journal and stays revertible — which is why `_erpgen`
lives here and not in the run loop.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from erpgen.client import ERPNextClient
from erpgen.customers_full import (
    detect_party_sheet,
    flat_map_for,
    flow_for_party,
    parse_flat_target,
    party_for_flow,
)
from erpgen.infer import guess_doctype
from erpgen.mapper import MappingEngine
from erpgen.metadata import fetch_with_children
from erpgen.overrides import DEFAULT_OVERRIDES, load_overrides, set_mapping
from erpgen.source import read_source
from erpgen.tools import (
    create_field,
    create_record,
    describe_doctype,
    get_record,
    list_records,
    update_record,
)

from .trace import _TRANSCRIPT_CTX

#: repository root — `_erpgen` runs the CLI from here
ROOT = Path(__file__).resolve().parents[2]

CLIENT: ERPNextClient = None  # set by the agent CLI before any tool runs

def _erpgen(args: list[str], timeout: int = 300) -> tuple[int, str]:
    res = subprocess.run(
        [sys.executable, str(ROOT / "erpgen.py"), *args],
        capture_output=True, text=True, cwd=ROOT, timeout=timeout,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def _j(v) -> str:
    return json.dumps(v, indent=2, default=str)


# ---------------------------------------------------------------- tools
def _latest_analysis(doctype: str, source: str = ""):
    """`latest_analysis` from the package root, resolved at call time.

    Imported lazily on purpose: the resolver lives in `erpgen.agent` (the run
    loop shares it and reads the same `ROOT`), so a module-level import here
    would be a cycle. Looking it up on the package each call also keeps
    `monkeypatch.setattr(erpgen.agent, "latest_analysis", ...)` able to intercept
    the tools, which is how the tests steer the mapping flow offline.
    """
    from erpgen.agent import latest_analysis

    return latest_analysis(doctype, source)


def t_latest_analysis(doctype: str, source: str = "") -> str:
    a = _latest_analysis(doctype, source)
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
    fresh = _latest_analysis(dt, source) if dt else None
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


def t_update_record(doctype: str, name: str, fields_json: str) -> str:
    try:
        fields = json.loads(fields_json)
        return _j(update_record(CLIENT, doctype, name, fields))
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


def t_correct(source: str, doctype: str, correction_json: str,
              conflict: str = "") -> str:
    """Record one worksheet correction; the CLI journals it so revert revokes it.

    `conflict` is accepted for leniency: the tool description asks for it inside
    `correction_json`, but the model sometimes passes it top-level too. When it
    does (and the correction does not already name one), merge it in rather than
    failing the call.
    """
    try:
        corr = json.loads(correction_json)
    except json.JSONDecodeError as e:
        return _j({"error": f"correction_json is not valid JSON: {e}"})
    if conflict and isinstance(corr, dict) and not corr.get("conflict"):
        corr["conflict"] = conflict
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
                    "(conflicts, mappings, suggested custom fields). Pass source "
                    "to disambiguate when several sheets share one doctype."},
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
    {"fn": t_update_record, "name": "update_record",
     "description": "Change fields on an EXISTING record (identified by its name). "
                    "Pass fields as JSON. Use it when a record the import depends "
                    "on is in the way — a leaf that must become a group, a wrong "
                    "parent, a value that is already set. Revertible: the previous "
                    "values are journalled. It cannot rename a record."},
    {"fn": t_set_mapping, "name": "set_mapping",
     "description": "Record a forced source-column -> target-field mapping override "
                    "for a doctype. Fixes ambiguous/missed mappings. For flat party "
                    "sheets use doctype='customers_full'|'suppliers_full' and "
                    "target='<customer|supplier|contact|address>.<fieldname>' "
                    "(e.g. customer.tax_id, supplier.tax_id)."},
    {"fn": t_correct, "name": "correct",
     "description": "Record ONE worksheet correction for a source sheet. Actions "
                    "and their exact JSON fields (a row ref is the key value as a "
                    "string, or {\"row\": N} for a source line number, 1 = header):\n"
                    "- set_value: {\"action\":\"set_value\",\"at\":<ref>,\"column\":\"<col>\",\"value\":\"<v>\",\"conflict\":\"<key>\"}\n"
                    "- skip_row: {\"action\":\"skip_row\",\"at\":<ref>,\"reason\":\"...\",\"conflict\":\"<key>\"}\n"
                    "- merge_rows: {\"action\":\"merge_rows\",\"keep\":<ref>,\"drop\":[<ref>,...],\"field_overrides\":{\"<col>\":\"<v>\"},\"conflict\":\"<key>\"}\n"
                    "- dismiss_conflict: {\"action\":\"dismiss_conflict\",\"conflict\":\"<key>\",\"reason\":\"...\"}\n"
                    "- change_key: {\"action\":\"change_key\",\"column\":\"<col>\",\"reason\":\"...\"}\n"
                    "The conflict key is \"<kind>:<source or field>[:<target>]\" "
                    "(e.g. \"duplicate_row:Customer Name:customer_name\")."},
    {"fn": t_describe_doctype, "name": "describe_doctype",
     "description": "Summarize a doctype's structure (required fields, links, "
                    "child tables, fetch_from, id field)."},
    {"fn": t_get_record, "name": "get_record",
     "description": "Fetch a single record by name as JSON."},
    {"fn": t_list_records, "name": "list_records",
     "description": "List records of a doctype (optional ERPNext filters JSON)."},
]
