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
        return True, f"{url} reachable (HTTP {e.code}) — check --api-base"
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


def _is_llm_error(exc: BaseException) -> bool:
    """True for OpenAI/httpx API failures, as opposed to a bug in our own code."""
    if type(exc).__module__.split(".")[0] in ("openai", "httpx", "httpcore"):
        return True
    name = type(exc).__name__.lower()
    return any(k in name for k in ("apierror", "connection", "timeout", "ratelimit",
                                   "authentication", "permissiondenied", "notfound"))


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
    text = _text(getattr(result, "response", None)) or _text(getattr(result, "raw", None))
    if not text.strip():
        calls = getattr(result, "tool_calls", None) or []
        text = (f"(no text response; the agent ended its turn with "
                f"{len(calls)} tool call(s) — see the transcript)") if calls \
            else "(no text response)"
    return text


async def run_agent(args) -> int:
    flat = False
    if args.source:
        src = read_source(args.source)
        party = detect_party_sheet(src)
        if party:
            flat = True
            doctype = flow_for_party(party)
        else:
            doctype = args.doctype or guess_doctype(src)
    else:
        doctype = args.doctype or None

    llm = get_llm(args.provider, args.model, args.api_base)

    # fail fast with a useful message instead of a transport traceback mid-loop
    if args.provider != "openai":
        base = args.api_base or "https://api.deepseek.com"
        key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        ok, detail = llm_preflight(base, key)
        if not ok:
            # the probe knows *why*; the shared help block knows the next steps
            print(f"ERROR: LLM endpoint unreachable — {detail}", file=sys.stderr)
            print(describe_llm_error(ConnectionError(detail), base,
                                     args.model or "", args.provider),
                  file=sys.stderr)
            return 2
        print(f"LLM preflight: {detail}")

    # ensure we have an analysis to work from
    if args.analysis:
        a = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    elif args.source:
        cmd = ["map", args.source]
        if doctype and not flat:
            cmd += ["--doctype", doctype]
        if args.defaults:
            cmd += ["--defaults", args.defaults]
        _erpgen(cmd)
        a = latest_analysis(doctype)
        if a is None:
            print("ERROR: map produced no analysis", file=sys.stderr)
            return 2
    else:
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

    # journal every effect this run applies (custom fields, records) so the whole
    # run is revertible with one command: erpgen.py revert <journal>
    from erpgen import tools as erpgen_tools  # noqa: PLC0415

    # with --run the agent joins the same unified context as the CLI commands:
    # the mapper's conflicts become the run's requirements, and the agent's tool
    # calls satisfy them reactively. Without it, fall back to a per-run journal.
    if getattr(args, "run", None):
        from erpgen.context import MigrationContext  # noqa: PLC0415
        journal = MigrationContext(args.run, logs_dir, source=source or "agent",
                                   base_url=args.base, doctypes=[doctype],
                                   command="agent")
        seeded = journal.add_requirements(a["conflicts"])
        log_event(event="run_context_open", path=str(journal.path), requirements=seeded)
        print(f"Run context: {journal.path}  ({seeded} requirement(s) recorded)")
    else:
        from erpgen.journal import MigrationJournal  # noqa: PLC0415
        journal = MigrationJournal(logs_dir, doctype=doctype, source=source or "agent",
                                   base_url=args.base)
        log_event(event="journal_open", path=str(journal.path))
    erpgen_tools.ACTIVE_JOURNAL = journal

    converged = False
    final_response = ""
    after_errs: list = []
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
        try:
            final_response = await _run_agent_round(workflow, user_msg)
        except Exception as e:  # noqa: BLE001 — classify, don't dump a traceback
            if not _is_llm_error(e):
                raise
            base = args.api_base or "https://api.deepseek.com"
            print("\n" + describe_llm_error(e, base, args.model or "", args.provider),
                  file=sys.stderr)
            log_event(event="llm_error", round=round_no, error=f"{type(e).__name__}: {e}")
            fh.close()
            return 2

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
            converged = True
            break
        prev_error_count = len(after_errs)
    else:
        print(f"\nReached max rounds ({args.max_rounds}) with unresolved error conflicts.")
        print(final_response)

    log_event(event="run_end", max_rounds=args.max_rounds, resolved=converged)
    fh.close()
    erpgen_tools.ACTIVE_JOURNAL = None

    is_context = hasattr(journal, "pending_requirements")   # unified run context
    if is_context:
        pend = journal.pending_requirements()
        print(f"\nRun context: {journal.path}  ({journal.effects} effect(s), "
              f"{len(pend)} requirement(s) still pending)")
        for r in pend:
            print(f"    PENDING  {r.get('kind')}: {r.get('detail')}")
        journal.close(status="ok")
        print(f"  revert the whole run: python3 erpgen.py revert {journal.run_id} --apply")
    else:
        journal.close()
        print(f"\nJournal: {journal.path}  ({journal.count} revertible effect(s))")
        print(f"  revert with: python3 erpgen.py revert {journal.path}")

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
    ap.add_argument("--run", metavar="RUN_ID",
                    help="join a unified migration run context (logs/run-<id>.jsonl) so "
                         "mapper requirements, agent fixes and revert all share one log")
    ap.add_argument("--max-rounds", type=int, default=20,
                    help="outer convergence loop cap (default: 20)")
    ap.add_argument("--doctor", action="store_true",
                    help="show tools + analysis without calling an LLM")
    args = ap.parse_args()

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


if __name__ == "__main__":
    raise SystemExit(main())
