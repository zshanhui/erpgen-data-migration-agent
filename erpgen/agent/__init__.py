#!/usr/bin/env python3
"""The LLM agent — the run loop, the convergence policy and the CLI.

`erpgen agent ...` lands here (see `add_agent_flags`); there is no separate
script. Reads the latest mapping analysis for a doctype, then an LLM agent
resolves the conflicts using tools and loops until error-severity conflicts are
gone, then imports and verifies.

Layout — this module keeps the *deciding*, the submodules keep the *plumbing*:

  erpgen/agent/tools.py   the eleven tools the model may call, and the registry
  erpgen/agent/trace.py   live progress on stdout + per-call LLM instrumentation
  erpgen/agent/__init__.py  (here) the round loop, stall detection, data-quality
                          flow, reporting, journal wiring and argv handling
  erpgen/llm_providers.py which provider, endpoint, credential and model

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
deepseek-flash (V4.1 Flash) on https://api.deepseek.com (--api-base to override),
and accepts --model flash|pro as shorthands for deepseek-flash / deepseek-v4-pro.
DeepInfra takes full model ids only (no shorthands), e.g.
--model Qwen/Qwen3.8-Flash; it defaults to the GLM-5.3 flagship zai-org/GLM-5.3.
The DeepInfra ids checked against /v1/models are listed in DEEPINFRA_KNOWN_MODELS
and printed by --doctor; any other id the provider serves works too.
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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from erpgen.client import ERPNextClient  # noqa: E402
from erpgen.customers_full import detect_party_sheet, flow_for_party  # noqa: E402
from erpgen.infer import guess_doctype  # noqa: E402
# Every provider fact lives in erpgen.llm_providers; only what the run loop
# actually calls is imported here. It must come in *by name* so that
# `monkeypatch.setattr(agent, "get_llm", ...)` still intercepts the call.
from erpgen.llm_providers import (  # noqa: E402
    DEEPINFRA_KNOWN_MODELS,
    describe_llm_error,
    get_llm,
)
from erpgen.llm_providers import _is_llm_error, _llm_base, _preflight_llm  # noqa: E402
from erpgen.overrides import DEFAULT_OVERRIDES  # noqa: E402
from erpgen.prompts import SYSTEM_PROMPT, correction_proposal_prompt  # noqa: E402
from erpgen.source import read_source  # noqa: E402

# The package facade. `__init__` re-exports its own submodules so that
# `erpgen.agent.<name>` keeps working for the CLI and the tests, which reach for
# the tools and the tracer through the package rather than by deep path. Names
# the loop itself uses are called here; the rest are the deliberate surface.
from erpgen.agent import tools as _tools  # noqa: E402
from erpgen.agent.tools import (  # noqa: E402
    TOOLS,
    _erpgen,
    import_failure_count,
    t_correct,
    t_latest_analysis,
    t_run_import,
    t_run_map,
    warning_digest,
)
from erpgen.agent.trace import (  # noqa: E402
    _LIVE,
    _TRANSCRIPT_CTX,
    _brief,
    _brief_args,
    _live,
    _log_llm_event,
    _requested_tools,
    _safe,
    _text,
    _thinking,
    _wrap_tool,
    instrumented_llm,
    llm_usage,
)

#: Re-exported on purpose: a package `__init__` is the facade for its own
#: submodules, and the CLI and the tests reach for these as `erpgen.agent.<name>`
#: rather than by deep path. Listed so the re-export reads as deliberate instead
#: of as imports nobody got round to using.
__all__ = [
    "TOOLS", "_erpgen", "import_failure_count", "t_correct",
    "t_latest_analysis", "t_run_import", "t_run_map", "warning_digest",
    "_LIVE", "_TRANSCRIPT_CTX", "_brief", "_brief_args", "_live",
    "_log_llm_event", "_requested_tools", "_safe", "_text", "_thinking",
    "_wrap_tool", "instrumented_llm", "llm_usage",
]


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


def _is_iteration_exhausted(exc: BaseException) -> bool:
    """True when the workflow stopped because its internal step budget ran out."""
    name = type(exc).__name__
    if "WorkflowRuntimeError" in name or "MaxIterations" in name:
        return True
    return "max iterations" in str(exc).lower()


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
    print(f"\nVetted deepinfra models (--model <full id>; any other id also works):")
    for mid, note in DEEPINFRA_KNOWN_MODELS.items():
        print(f"  - {mid:<24} {note}")
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


def _extract_json(text: str) -> Optional[dict]:
    """The first JSON object in an LLM reply, tolerating fences and prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            ch = text[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        break
    return None


def _rows_text(conflict: dict, src) -> str:
    """The source rows a conflict names, one readable line each."""
    rows: set = set()
    for g in conflict.get("groups") or []:
        rows.update(g.get("rows") or [])
    rows.update(conflict.get("rows") or [])
    out = []
    for line in sorted(rows):
        idx = line - 2  # line 1 = header
        if 0 <= idx < src.n_rows:
            cells = " | ".join(
                f"{h}={src.rows[idx][j] if j < len(src.rows[idx]) else ''}"
                for j, h in enumerate(src.headers))
            out.append(f"  line {line}: {cells}")
    return "\n".join(out)


def _ref_label(ref, src=None, key_column: str = "") -> str:
    """A row reference as an operator reads it: 'row 31 (Hollow Core Drilling)'."""
    if isinstance(ref, dict) and isinstance(ref.get("row"), int):
        line = ref["row"]
        label = f"row {line}"
        if src is not None and key_column in src.headers:
            idx = line - 2
            col = src.headers.index(key_column)
            if 0 <= idx < src.n_rows and col < len(src.rows[idx]):
                key = str(src.rows[idx][col]).strip()
                if key:
                    label += f" ({key})"
        return label
    return repr(ref)


def _describe_proposal(corr: dict, src=None, key_column: str = "") -> str:
    """A correction proposal as a plain sentence, not raw JSON."""
    action = corr.get("action")
    if action == "skip_row":
        why = f" — {corr['reason']}" if corr.get("reason") else ""
        return f"drop {_ref_label(corr.get('at'), src, key_column)}{why}"
    if action == "set_value":
        return (f"set {corr.get('column')} on "
                f"{_ref_label(corr.get('at'), src, key_column)} "
                f"to {corr.get('value')!r}")
    if action == "merge_rows":
        drops = ", ".join(_ref_label(d, src, key_column)
                          for d in (corr.get("drop") or []))
        return f"merge {drops} into {_ref_label(corr.get('keep'), src, key_column)}"
    if action == "dismiss_conflict":
        why = f" — {corr['reason']}" if corr.get("reason") else ""
        return f"waive {corr.get('conflict')}{why}"
    if action == "change_key":
        return f"use {corr.get('column')} as the review key"
    return json.dumps(corr, ensure_ascii=False)


async def _propose_correction(llm, conflict: dict, src, key_column: str,
                              conflict_key: str) -> Optional[dict]:
    """One LLM proposal for one conflict; None when it produced nothing usable."""
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    prompt = correction_proposal_prompt(
        key_column, conflict_key,
        json.dumps(conflict, indent=2, default=str),
        _rows_text(conflict, src))
    # the raw model JSON is not shown live; the loop renders it as a sentence
    _LIVE["echo_content"] = False
    try:
        response = await llm.achat([ChatMessage(role=MessageRole.USER, content=prompt)])
    finally:
        _LIVE["echo_content"] = True
    msg = getattr(response, "message", None)
    text = getattr(msg, "content", None) or str(response)
    return _extract_json(text)


def _correction_sink(args, doctype: str, source: str):
    """Where a data-quality correction is journaled: the run context, or a journal."""
    log_dir = getattr(args, "log_dir", "logs")
    if getattr(args, "run", None):
        from erpgen.context import MigrationContext  # noqa: PLC0415

        return MigrationContext(args.run, log_dir, source=source or "agent",
                                base_url=getattr(args, "base", ""),
                                doctypes=[doctype], command="correct")
    from erpgen.journal import MigrationJournal  # noqa: PLC0415

    return MigrationJournal(log_dir, doctype=doctype, source=source or "agent",
                            base_url=getattr(args, "base", ""))


async def _llm_data_quality_loop(args, analysis: dict, doctype: str,
                                 source: str, flat: bool) -> int:
    """LLM proposes a correction per data-quality error; the user approves each.

    Returns 0 when every blocker was corrected, EXIT_DATA_QUALITY when any
    remain (the mapping/import flow must not run), or 2 on a transport failure.
    """
    from erpgen.corrections import (WORKSHEET_DIR, add_correction, conflict_key,
                                   worksheet_path)

    blockers = _data_quality_blockers(analysis)
    if not blockers:
        return 0

    llm = get_llm(args.provider, args.model, args.api_base, wrap=instrumented_llm)
    if not _preflight_llm(args):
        return 2

    try:
        src = read_source(source) if source else None
    except Exception:  # noqa: BLE001 — row data is a nicety, not a requirement
        src = None
    key_column = (analysis.get("source") or {}).get("key_column") or ""
    ws_dir = getattr(args, "worksheet_dir", None) or WORKSHEET_DIR
    ws_path = worksheet_path(doctype, source, ws_dir)

    print(f"\n=== LLM data-quality correction — {len(blockers)} conflict(s) ===")
    for i, conf in enumerate(blockers, 1):
        print(f"\n[{i}/{len(blockers)}] {conf['kind']}: "
              f"{conf.get('source') or conf.get('field')}")
        proposal = None
        try:
            proposal = await _propose_correction(
                llm, conf, src, key_column, conflict_key(conf))
        except Exception as e:  # noqa: BLE001 — keep going to the next error
            print(f"  (proposal failed: {type(e).__name__}: {e})")
        if proposal is None:
            print("  (no usable proposal — skipping; fix this one by hand)")
            continue
        print(f"  Proposal: {_describe_proposal(proposal, src, key_column)}")
        ans = input("  Apply this correction? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Skipped.")
            continue
        try:
            saved, created = add_correction(ws_path, proposal, created_by="agent")
        except ValueError as e:
            print(f"  Rejected: {e}")
            continue
        if created:
            sink = _correction_sink(args, doctype, source)
            sink.effect("correction_add",
                        {"op": "correction_revoke", "path": str(ws_path),
                         "correction_id": saved["id"]},
                        doctype=doctype, source=source, correction_id=saved["id"])
            sink.close()
            print(f"  correction {saved['id']} recorded.")
        else:
            print(f"  correction {saved['id']} already recorded.")

    # re-detect; a corrected conflict now reads corrected/waived, a skipped one
    # stays open and must still block the mapping/import flow
    cmd = ["map", source]
    if doctype and not flat:
        cmd += ["--doctype", doctype]
    _erpgen(cmd)
    fresh = latest_analysis(doctype, source) or analysis
    remaining = _data_quality_blockers(fresh)
    if remaining:
        print(f"\n=== {len(remaining)} data-quality error(s) still remain — "
              "not proceeding to the mapping/import flow ===")
        for c in remaining:
            print(f"    [{c['kind']}] {c.get('source') or c.get('field')}")
        print("  Fix them by hand (erpgen.py correct ...) or re-run with "
              "--llm-data-quality-correction and approve the remaining proposals.")
        return EXIT_DATA_QUALITY
    print("\nData quality clean — proceeding to the mapping/import flow.")
    return 0


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
        "Fix them (create_field / create_record / update_record / set_mapping / "
        "correct), then run_map to confirm they are gone. Import only once no "
        "error-severity conflict is open or stale."
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
        fresh = latest_analysis(doctype, source)
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


def _revert_hint(run_id: str) -> None:
    """The one-liner that reverses an entire run, printed at the end."""
    print(f"  revert this run: python3 erpgen.py revert {run_id} --apply")


def _report_run_end(journal, transcript: Transcript) -> None:
    if hasattr(journal, "pending_requirements"):  # unified run context
        pending = journal.pending_requirements()
        print(f"\nRun context: {journal.path}  ({journal.effects} effect(s), "
              f"{len(pending)} requirement(s) still pending)")
        for r in pending:
            print(f"    PENDING  {r.get('kind')}: {r.get('detail')}")
        journal.close(status="ok")
        _revert_hint(journal.run_id)
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
        if getattr(args, "llm_data_quality_correction", False):
            code = await _llm_data_quality_loop(args, analysis, doctype, source, flat)
            if code:
                return code
            analysis = latest_analysis(doctype, source) or analysis
        else:
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
            print("  Or pass --llm-data-quality-correction to have the LLM propose "
                  "fixes you approve one by one.")
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
            if getattr(args, "run", None):
                _revert_hint(args.run)
            return 0
        pre_import = warning_digest(out) or out[-1500:]
        print(f"  {failed} row(s) failed — engaging the agent to investigate.")

    llm = get_llm(args.provider, args.model, args.api_base, wrap=instrumented_llm)
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
    ap.add_argument("--model", help="LLM model (provider default if omitted). "
                                    "deepseek accepts 'flash'|'pro' -> deepseek-flash "
                                    "/ deepseek-v4-pro; deepinfra takes the full id, "
                                    "e.g. zai-org/GLM-5.3-Flash or Qwen/Qwen3.8-Flash "
                                    "(--doctor lists the vetted ids)")
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
    ap.add_argument("--llm-data-quality-correction", action="store_true",
                    help="let the LLM propose a correction for each data-quality "
                         "error and apply it only after a y/N confirmation; if any "
                         "remain, the mapping/import flow does not run")


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

    # the tools own the client, so the CLI hands it to them rather than keeping
    # a second module-level copy that could drift from theirs
    _tools.CLIENT = ERPNextClient(args.base, username=args.user, password=args.password)

    base = _llm_base(args)
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
