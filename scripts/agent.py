#!/usr/bin/env python3
"""Agent skeleton — LlamaIndex AgentWorkflow driving the erpgen migration loop.

Reads the latest mapping analysis for a doctype, then an LLM agent resolves the
conflicts using tools (create fields, create missing records, set mapping
overrides, re-run map/import) and loops until error-severity conflicts are gone,
then imports and verifies.

Usage:
  # fresh analysis + agent run
  python3 scripts/agent.py --doctype Customer --source samples/customers.csv \
      --defaults '{"customer_group":"Commercial"}'

  # resume from an existing analysis
  python3 scripts/agent.py --analysis analysis/analysis-customer-<ts>.json

  # no LLM: build tools, show plan, exit (for wiring/debugging)
  python3 scripts/agent.py --doctor --doctype Customer --source samples/customers.csv

  # pick a provider and control the outer convergence loop
  python3 scripts/agent.py --doctype Customer --source samples/customers.csv \
      --provider deepseek --max-rounds 30

LLM provider: --provider openai|deepseek (auto-detected from
OPENAI_API_KEY / DEEPSEEK_API_KEY). DeepSeek defaults to model
deepseek-v4-flash on https://api.deepseek.com (--api-base to override).
Run with the project venv: .venv/bin/python scripts/agent.py ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from erpgen.client import ERPNextClient  # noqa: E402
from erpgen.infer import guess_doctype  # noqa: E402
from erpgen.overrides import DEFAULT_OVERRIDES, set_mapping  # noqa: E402
from erpgen.parties import is_parties_sheet  # noqa: E402
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


def latest_analysis(doctype: str):
    d = ROOT / "analysis"
    pat = f"analysis-{doctype.lower().replace(' ', '-')}-*.json"
    files = sorted(d.glob(pat))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _j(v) -> str:
    return json.dumps(v, indent=2, default=str)


def _text(value) -> str:
    """Normalize an agent result to plain text.

    llama-index may hand back a str, a ChatMessage, or an object with .content
    depending on how the workflow finishes — unwrap any of them safely.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if content is not None:
        return str(content)
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
    if doctype:
        cmd += ["--doctype", doctype]
    if defaults and defaults != "{}":
        cmd += ["--defaults", defaults]
    code, out = _erpgen(cmd)
    dt = doctype or guess_doctype(read_source(source))
    fresh = latest_analysis(dt) if dt else None
    if fresh is None:
        return f"map failed (exit {code}):\n{out[-1500:]}"
    return f"map exit {code}. Fresh analysis:\n{_j(fresh)}"


def t_run_import(source: str, doctype: str = "", apply: bool = False,
                 defaults: str = "{}") -> str:
    cmd = ["import", source]
    if doctype:
        cmd += ["--doctype", doctype]
    if defaults and defaults != "{}":
        cmd += ["--defaults", defaults]
    if apply:
        cmd.append("--apply")
    code, out = _erpgen(cmd, timeout=900)
    return f"import exit {code}:\n{out[-2000:]}"


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
        set_mapping(str(ROOT / DEFAULT_OVERRIDES), doctype, column, target)
        return _j({"saved": f"{doctype}.{column} -> {target}",
                   "file": str(ROOT / DEFAULT_OVERRIDES)})
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)})


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
                    "for a doctype. Fixes ambiguous/missed mappings."},
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

Per-iteration workflow:
1. Read the analysis from the user message or latest_analysis.
2. Resolve every error-severity conflict (create fields/records, set mappings).
3. run_map again and confirm the error-severity conflicts decreased. Repeat until zero.
4. run_import with apply=True.
5. Verify with get_record / list_records if useful, then give a final summary.

Rules:
- Never import while error-severity conflicts remain.
- Imports are idempotent: re-running is safe and skips existing records.
- Never modify source files; record decisions via set_mapping / create_field.
- Tools return JSON; reason over it before acting."""  # noqa: E501


# ---------------------------------------------------------------- llm
def _deepseek_llm(model: str, api_base: str):
    """OpenAI-compatible client for DeepSeek.

    Uses llama-index's OpenAILike (not the OpenAI class, whose metadata
    property validates model names against OpenAI's registry and rejects
    DeepSeek model ids). is_function_calling_model=True is required for the
    FunctionAgent tool loop.
    """
    from llama_index.llms.openai_like import OpenAILike

    return OpenAILike(
        model=model or "deepseek-v4-flash",
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        api_base=api_base or "https://api.deepseek.com",
        is_chat_model=True,
        is_function_calling_model=True,
    )


def get_llm(provider: str, model: str, api_base: str = ""):
    from llama_index.llms.openai import OpenAI

    if provider == "openai":
        return OpenAI(model=model or "gpt-4o-mini")
    if provider == "deepseek":
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise SystemExit(
                "DEEPSEEK_API_KEY is not set. export DEEPSEEK_API_KEY=... "
                "(or pass --provider openai)"
            )
        return _deepseek_llm(model, api_base)
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAI(model=model or "gpt-4o-mini")
    if os.environ.get("DEEPSEEK_API_KEY"):
        return _deepseek_llm(model, api_base)
    raise SystemExit(
        "No LLM provider configured. Set OPENAI_API_KEY or DEEPSEEK_API_KEY "
        "and choose --provider openai|deepseek."
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
        if log:
            log(event="tool_call", round=rnd, name=name,
                kwargs=_safe(kwargs if kwargs else args))
        result = fn(*args, **kwargs)
        if log:
            log(event="tool_result", round=rnd, tool=name,
                args=_safe(kwargs if kwargs else args),
                result_tail=_text(result)[-600:])
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
    )
    return AgentWorkflow(agents=[agent], root_agent="migration_agent", timeout=900)


# ---------------------------------------------------------------- main
def cmd_doctor(args) -> int:
    print(f"Agent doctor (no LLM call) — doctype={args.doctype}")
    a = latest_analysis(args.doctype)
    if a is None:
        print("  no analysis yet; run: python3 erpgen.py map <source> --doctype "
              f"{args.doctype}")
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


async def _run_agent_round(workflow, user_msg: str) -> str:
    """Run one agent round.

    Tool calls and results are logged by the tool wrappers via _TRANSCRIPT_CTX
    (set by run_agent before each round). Here we await the workflow — the
    WorkflowHandler is awaitable but NOT async-iterable in llama-index 0.14 —
    and return the final response text.
    """
    result = await workflow.run(user_msg=user_msg)
    return _text(getattr(result, "response", None)) or _text(result)


async def run_agent(args) -> int:
    # Flat party sheet (inline contact/address) has no single doctype and no
    # mapping conflicts — run the deterministic parties import directly.
    if args.source:
        src = read_source(args.source)
        if is_parties_sheet(src):
            print("Flat parties sheet detected — running deterministic import "
                  f"({'apply' if args.apply else 'dry-run'}).")
            cmd = ["import", args.source]
            if args.defaults:
                cmd += ["--defaults", args.defaults]
            if args.apply:
                cmd.append("--apply")
            code, out = _erpgen(cmd, timeout=900)
            print(out[-2500:])
            return 0 if code == 0 else 2
        doctype = args.doctype or guess_doctype(src)

    llm = get_llm(args.provider, args.model, args.api_base)

    # ensure we have an analysis to work from
    if args.analysis:
        a = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    elif args.source:
        cmd = ["map", args.source]
        if doctype:
            cmd += ["--doctype", doctype]
        if args.defaults:
            cmd += ["--defaults", args.defaults]
        _erpgen(cmd)
        a = latest_analysis(doctype or guess_doctype(src))
        if a is None:
            print("ERROR: map produced no analysis", file=sys.stderr)
            return 2
    else:
        doctype = args.doctype or None
        a = latest_analysis(doctype) if doctype else None
        if a is None:
            print("ERROR: no analysis found. Pass --source or --analysis.", file=sys.stderr)
            return 2

    print(f"Starting agent for {a['doctype']} — {len(a['conflicts'])} conflicts")
    for c in a["conflicts"]:
        print(f"  [{c['severity']:<7}] {c['kind']:<22} {c.get('source') or c.get('field')}")

    workflow = build_workflow(llm)
    doctype = a["doctype"]
    source = a.get("source") or args.source or "unknown"
    prev_error_count = None
    final_response = ""
    after_errs: list = []

    # monotonic audit transcript: run_start, thinking/tool events, rounds, run_end
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    tfile = logs_dir / f"agent-{doctype.lower().replace(' ', '-')}-{stamp}.jsonl"
    fh = tfile.open("w", encoding="utf-8")

    def log_event(**entry) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        fh.write(json.dumps(row, default=str) + "\n")
        fh.flush()

    log_event(event="run_start", doctype=doctype, source=source, base=args.base,
              provider=args.provider, model=args.model or "(provider default)",
              max_rounds=args.max_rounds)

    for round_no in range(1, args.max_rounds + 1):
        errs = [c for c in a["conflicts"] if c["severity"] == "error"]
        print(f"\n=== Round {round_no}/{args.max_rounds} — {len(errs)} error conflict(s), "
              f"{len(a['conflicts'])} total ===")
        for c in errs:
            print(f"    [error] {c['kind']}: {c.get('source') or c.get('field')}")

        if round_no == 1:
            user_msg = (
                f"Resolve the migration conflicts for doctype '{doctype}' and import the data.\n"
                f"Source file: {source}\n"
                f"Base URL: {a.get('base_url', '')}\n\n"
                f"Current analysis:\n{json.dumps(a, indent=2, default=str)}"
            )
        else:
            remaining = [c for c in a["conflicts"] if c["severity"] == "error"]
            user_msg = (
                f"Round {round_no}. These error-severity conflicts REMAIN after your last "
                f"round:\n{json.dumps(remaining, indent=2, default=str)}\n\n"
                "Fix them (create_field / create_record / set_mapping), then run_map to "
                "confirm they are gone. Import only once no error conflicts remain."
            )

        log_event(event="round_start", round=round_no, errors_before=len(errs),
                  conflicts_total=len(a["conflicts"]))
        _TRANSCRIPT_CTX.update({"round": round_no, "log_event": log_event})
        final_response = await _run_agent_round(workflow, user_msg)

        # programmatic verification: re-read the newest analysis artifact
        fresh = latest_analysis(doctype)
        if fresh is not None:
            a = fresh
        after_errs = [c for c in a["conflicts"] if c["severity"] == "error"]

        note = ""
        if prev_error_count is not None and len(after_errs) >= prev_error_count:
            note = "  [no decrease vs previous round]"
        print(f"--- Round {round_no}: {len(after_errs)} error(s) remain{note}")
        log_event(event="round_end", round=round_no, errors_before=len(errs),
                  errors_after=len(after_errs),
                  conflicts_total=len(a["conflicts"]),
                  response=final_response[-2000:])

        if not after_errs:
            print("\n=== AGENT CONVERGED (no error-severity conflicts) ===\n")
            print(final_response)
            break
        prev_error_count = len(after_errs)
    else:
        print(f"\nReached max rounds ({args.max_rounds}) with unresolved error conflicts.")
        print(final_response)

    log_event(event="run_end", max_rounds=args.max_rounds,
              resolved=len(after_errs) == 0)
    fh.close()
    print(f"\nAgent transcript (thinking + tool calls + responses): {tfile}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="agent.py", description=__doc__)
    ap.add_argument("--doctype", default="Customer")
    ap.add_argument("--source", help="source CSV/XLSX to analyze (runs map first)")
    ap.add_argument("--analysis", help="path to an existing analysis JSON")
    ap.add_argument("--defaults", help='JSON defaults for map/import, e.g. \'{"customer_group":"Commercial"}\'')
    ap.add_argument("--base", default="http://localhost:8082")
    ap.add_argument("--user", default="Administrator")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--provider", default="auto",
                    choices=["auto", "openai", "deepseek"])
    ap.add_argument("--model", help="LLM model (provider default if omitted)")
    ap.add_argument("--api-base", help="OpenAI-compatible base URL "
                                       "(DeepSeek default: https://api.deepseek.com)")
    ap.add_argument("--max-rounds", type=int, default=20,
                    help="outer convergence loop cap (default: 20)")
    ap.add_argument("--apply", action="store_true",
                    help="flat sheets: import immediately instead of dry-run")
    ap.add_argument("--doctor", action="store_true",
                    help="show tools + analysis without calling an LLM")
    args = ap.parse_args()

    if args.doctor:
        # doctor only inspects files + tools — no ERPNext connection needed
        return cmd_doctor(args)

    global CLIENT
    CLIENT = ERPNextClient(args.base, username=args.user, password=args.password)
    return asyncio.run(run_agent(args))


if __name__ == "__main__":
    raise SystemExit(main())
