"""P0: LLM endpoint preflight.

The OpenAI SDK reports DNS failures, TLS verification errors, a dead proxy and a
refused connection *identically* — as a bare `APIConnectionError` with no clue
which. `run_agent` therefore probes the endpoint first and turns that into an
actionable message. Pure: the network calls are injected through the
`resolve` / `open_url` seams, so none of this touches the network.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erpgen import llm_providers as P  # provider logic lives here
from erpgen.agent import tools as agent_tools


# ------------------------------------------------------------------- fakes
def _resolve_ok(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _resolve_fail(host, port, **kw):
    raise OSError(8, "nodename nor servname provided, or not known")


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _open_status(code):
    def _open(req, timeout=None):
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)
        return _Resp(code)
    return _open


def _open_raises(exc):
    def _open(req, timeout=None):
        raise exc
    return _open


# ------------------------------------------------------------------- tests
def test_dns_failure_is_reported_with_proxy_hints(agent_mod, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    ok, detail = P.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_fail)
    assert ok is False
    assert "DNS lookup failed" in detail
    assert "127.0.0.1:9" in detail, "proxy env vars must be surfaced"


def test_reachable_401_means_the_key_is_the_problem(agent_mod, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ok, detail = P.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(401))
    assert ok is True, "401 proves the network path works"
    assert "check the API key" in detail


def test_reachable_403_is_also_a_key_problem(agent_mod):
    ok, detail = P.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(403))
    assert ok is True and "check the API key" in detail


def test_root_404_is_reachable_and_does_not_blame_api_base(agent_mod):
    """An API root routinely has no handler; DNS+TCP+TLS all succeeded, so
    telling the user to check --api-base is misleading noise."""
    ok, detail = P.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(404))
    assert ok is True
    assert "reachable" in detail and "404" in detail
    assert "--api-base" not in detail


def test_200_is_reachable(agent_mod):
    ok, detail = P.llm_preflight("https://api.deepseek.com", "k",
                                         resolve=_resolve_ok,
                                         open_url=_open_status(200))
    assert ok is True and "HTTP 200" in detail


def test_transport_error_reports_resolved_ips(agent_mod, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ok, detail = P.llm_preflight(
        "https://api.deepseek.com", "k", resolve=_resolve_ok,
        open_url=_open_raises(OSError("connection refused")))
    assert ok is False
    assert "93.184.216.34" in detail, "the IP actually reached must be shown"
    assert "proxies in env: none" in detail


def test_bare_host_is_upgraded_to_https(agent_mod):
    seen = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(200)

    P.llm_preflight("api.deepseek.com", "k", resolve=_resolve_ok,
                            open_url=_open)
    assert seen["url"] == "https://api.deepseek.com"


def test_api_key_is_sent_as_a_bearer_header(agent_mod):
    seen = {}

    def _open(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _Resp(200)

    P.llm_preflight("https://api.deepseek.com", "sk-secret",
                            resolve=_resolve_ok, open_url=_open)
    assert seen["auth"] == "Bearer sk-secret"


# ------------------------------------------------------- doctype resolution
def _sheet(tmp_path, name, header, row):
    p = tmp_path / name
    p.write_text(f"{header}\n{row}\n", encoding="utf-8")
    return str(p)


def test_doctype_is_inferred_from_the_source_headers(agent_mod, tmp_path):
    src = _sheet(tmp_path, "items.csv", "Item Code,Item Name,Group,UoM,Rate",
                 "MFG-1,Bearing,Products,Nos,45.5")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype=None)) == ("Item", False)


def test_explicit_doctype_overrides_inference(agent_mod, tmp_path):
    src = _sheet(tmp_path, "items.csv", "Item Code,Item Name", "MFG-1,Bearing")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype="Customer")) == ("Customer", False)


def test_flat_party_sheet_resolves_to_its_flow(agent_mod, tmp_path):
    src = _sheet(tmp_path, "suppliers-smb.csv",
                 "Supplier Name,Supplier Type,Supplier Group,Contact Name,Email",
                 "Acme,Company,Raw Material,Alicia,alicia@acme.example")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype=None)) == ("suppliers_full", True)
    src2 = _sheet(tmp_path, "customers-smb.csv",
                  "Customer Name,Customer Type,Group,Contact Name,Email",
                  "Acme,Company,Commercial,Alicia,alicia@acme.example")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src2, doctype=None)) == ("customers_full", True)


def test_unrecognised_sheet_resolves_to_none_not_a_guess(agent_mod, tmp_path):
    src = _sheet(tmp_path, "mystery.csv", "Foo,Bar", "1,2")
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=src, doctype=None)) == (None, False)


def test_no_source_no_doctype_resolves_to_none(agent_mod):
    assert agent_mod.resolve_doctype(
        argparse.Namespace(source=None, doctype=None)) == (None, False)


def test_doctype_flag_has_no_argparse_default(agent_mod):
    """Regression: `default="Customer"` made `args.doctype or guess_doctype(...)`
    always short-circuit, so EVERY single-doctype sheet was mapped against
    Customer (Item columns analysed as customer_name/customer_type)."""
    args = agent_mod.build_parser().parse_args(["--source", "samples/items.csv"])
    assert args.doctype is None, "a default here silently disables inference"


def test_max_iterations_flag_is_exposed(agent_mod):
    args = agent_mod.build_parser().parse_args([])
    assert args.max_iterations >= 20, "llama-index's own default of 20 is too low"


# ----------------------------------------------------- iteration exhaustion
class WorkflowRuntimeError(Exception): pass


def test_iteration_exhaustion_is_recognized_by_name(agent_mod):
    assert agent_mod._is_iteration_exhausted(WorkflowRuntimeError("boom")) is True


def test_iteration_exhaustion_is_recognized_by_message(agent_mod):
    assert agent_mod._is_iteration_exhausted(
        RuntimeError("Max iterations of 20 reached!")) is True


def test_iteration_exhaustion_ignores_unrelated_errors(agent_mod):
    assert agent_mod._is_iteration_exhausted(ValueError("nope")) is False


def test_iteration_exhaustion_is_not_mistaken_for_a_network_error(agent_mod):
    """It must not be swallowed by the LLM help block."""
    assert agent_mod._is_llm_error(WorkflowRuntimeError("Max iterations of 20 reached!")) is False


# ------------------------------- deterministic-first dispatch (no conflicts)
CLEAN = {"doctype": "Item", "conflicts": [], "source": "samples/items.csv",
         "base_url": "http://localhost:8082"}


def _run_args(**kw):
    ns = argparse.Namespace(
        source="samples/items.csv", analysis=None, doctype=None, defaults=None,
        base="http://localhost:8082", user="Administrator", password="admin",
        provider="deepseek", model=None, api_base=None, run=None, quiet=True,
        always_llm=False, max_rounds=20, max_iterations=50, max_stall_rounds=2,
        doctor=False)
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


class _T:
    logs_dir = Path("/tmp")
    path = Path("/tmp/agent-t.jsonl")

    def log(self, **k):
        pass

    def close(self):
        pass


def _stub_loop(agent_mod, monkeypatch, captured):
    """Replace the LLM-loop plumbing so dispatch logic can be tested alone."""
    monkeypatch.setattr(agent_mod, "build_workflow", lambda llm: object())
    monkeypatch.setattr(agent_mod.Transcript, "open",
                        classmethod(lambda cls, dt: _T()))
    monkeypatch.setattr(agent_mod, "_open_journal", lambda *a, **k: _T())
    monkeypatch.setattr(agent_mod, "_report_run_end", lambda *a: None)
    monkeypatch.setattr(agent_mod, "_preflight_llm", lambda a: True)

    async def _fake_rounds(args, wf, transcript, analysis, doctype, source,
                           pre_import=""):
        captured["pre_import"] = pre_import
        captured["rounds"] = True
        return agent_mod.RunOutcome(converged=True)

    monkeypatch.setattr(agent_mod, "_run_rounds", _fake_rounds)


@pytest.mark.parametrize("out,expected", [
    ("REST upsert: created 29, failed 0", 0),
    ("REST upsert: created 0, failed 29", 29),
    ("  customer   created 2 | skipped 0 | failed 1\n  address  created 2 | skipped 0", 1),
    ("  supplier   created 15 | skipped 0", 0),
    ('{"doctype": "Item", "failed": 3}', 3),
    ("Import summary\n  created: 0 | skipped: 0 | failed: 7", 7),
    ("nothing relevant", 0),
    # summary truncated away -> fall back to counting the row warnings
    ("WARNING: row 2: Customer 'A' failed: boom\nWARNING: row 3: Customer 'B' failed: boom", 2),
])
def test_import_failure_count_parses_every_output_shape(agent_mod, out, expected):
    assert agent_mod.import_failure_count(out) == expected


def test_no_conflicts_imports_without_any_llm_call(agent_mod, monkeypatch):
    """The point: a clean re-run must cost zero remote calls."""
    import asyncio
    used = []
    monkeypatch.setattr(agent_mod, "_load_analysis", lambda *a: dict(CLEAN))
    monkeypatch.setattr(agent_mod, "_erpgen",
                        lambda cmd, timeout=300: (0, "REST upsert: created 3, failed 0"))
    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: used.append(1))
    assert asyncio.run(agent_mod.run_agent(_run_args())) == 0
    assert used == [], "must not build an LLM when there is nothing to decide"


def test_pre_import_carries_the_run_id_before_the_subcommand(agent_mod, monkeypatch):
    import asyncio
    seen = {}
    monkeypatch.setattr(agent_mod, "_load_analysis", lambda *a: dict(CLEAN))
    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: None)

    def _fake_erpgen(cmd, timeout=300):
        seen["cmd"] = cmd
        return 0, "REST upsert: created 0, failed 0"

    monkeypatch.setattr(agent_mod, "_erpgen", _fake_erpgen)
    asyncio.run(agent_mod.run_agent(_run_args(run="r1")))
    assert seen["cmd"][:2] == ["--run", "r1"], "--run is a global flag (pre-subcommand)"
    assert seen["cmd"][2] == "import" and "--apply" in seen["cmd"]


def test_deterministic_import_prints_the_revert_command(agent_mod, monkeypatch, capsys):
    import asyncio

    monkeypatch.setattr(agent_mod, "_load_analysis", lambda *a: dict(CLEAN))
    monkeypatch.setattr(agent_mod, "_erpgen",
                        lambda cmd, timeout=300: (0, "REST upsert: created 0, failed 0"))
    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: None)
    assert asyncio.run(agent_mod.run_agent(_run_args(run="r1"))) == 0
    assert "revert this run: python3 erpgen.py revert r1 --apply" in capsys.readouterr().out


def test_row_failures_engage_the_agent_with_the_context(agent_mod, monkeypatch):
    import asyncio
    captured = {}
    monkeypatch.setattr(agent_mod, "_load_analysis", lambda *a: dict(CLEAN))
    monkeypatch.setattr(
        agent_mod, "_erpgen",
        lambda cmd, timeout=300: (0, _warnings(range(2, 31), SCHEMA_ERR)))
    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: object())
    _stub_loop(agent_mod, monkeypatch, captured)
    assert asyncio.run(agent_mod.run_agent(_run_args())) == 0
    assert captured.get("rounds") is True, "the agent must be engaged"
    assert "lead_time_days" in captured["pre_import"], \
        "the failure reason must be handed to the model"


def test_always_llm_skips_the_deterministic_pre_import(agent_mod, monkeypatch):
    import asyncio
    captured = {}
    ran = []
    monkeypatch.setattr(agent_mod, "_load_analysis", lambda *a: dict(CLEAN))
    monkeypatch.setattr(agent_mod, "_erpgen",
                        lambda cmd, timeout=300: ran.append(cmd) or (0, ""))
    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: object())
    _stub_loop(agent_mod, monkeypatch, captured)
    asyncio.run(agent_mod.run_agent(_run_args(always_llm=True)))
    assert ran == [], "no pre-import when --always-llm"
    assert captured.get("rounds") is True


def test_conflicts_still_go_straight_to_the_agent(agent_mod, monkeypatch):
    import asyncio
    captured = {}
    busy = {"doctype": "Customer", "source": "s.csv", "base_url": "u",
            "conflicts": [{"severity": "error", "kind": "required_missing",
                           "field": "customer_type"}]}
    monkeypatch.setattr(agent_mod, "_load_analysis", lambda *a: dict(busy))
    monkeypatch.setattr(agent_mod, "_erpgen",
                        lambda cmd, timeout=300: pytest.fail("must not pre-import"))
    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: object())
    _stub_loop(agent_mod, monkeypatch, captured)
    asyncio.run(agent_mod.run_agent(_run_args()))
    assert captured.get("rounds") is True
    assert captured["pre_import"] == ""


def test_round_message_includes_the_pre_import_failures(agent_mod):
    msg = agent_mod._round_message(1, "Customer", "s.csv", dict(CLEAN),
                                   pre_import="x29  Unknown column")
    assert "already run" in msg and "Unknown column" in msg


def test_round_message_omits_the_note_when_nothing_failed(agent_mod):
    assert "already run" not in agent_mod._round_message(1, "Item", "s.csv", dict(CLEAN))


# ------------------------------------------- live progress on stdout
@pytest.fixture
def live(agent_mod):
    """Ensure live output is on, and restore it afterwards."""
    agent_mod._LIVE["enabled"] = True
    yield agent_mod._LIVE
    agent_mod._LIVE["enabled"] = True


def test_live_prints_to_stdout_when_enabled(agent_mod, live, capsys):
    agent_mod._live("hello")
    assert "hello" in capsys.readouterr().out


def test_live_is_silent_when_disabled(agent_mod, live, capsys):
    live["enabled"] = False
    agent_mod._live("hello")
    assert capsys.readouterr().out == ""


def test_live_is_silent_when_disabled_for_llm_calls(agent_mod, live, capsys):
    """--quiet must silence the per-call stream too, not just _live()."""
    import asyncio
    live["enabled"] = False
    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod._TRANSCRIPT_CTX.update({"round": 1, "log_event": lambda **e: None})
    asyncio.run(agent_mod.instrumented_llm(_FakeLLM)().achat(messages=["a"]))
    assert "llm #" not in capsys.readouterr().out


def test_brief_truncates_long_values(agent_mod):
    assert agent_mod._brief("x" * 500, 20).endswith("…")
    assert len(agent_mod._brief("x" * 500, 20)) == 20
    assert agent_mod._brief("short") == "short"


def test_brief_collapses_newlines(agent_mod):
    assert agent_mod._brief("a\nb\tc") == "a b c"


def test_brief_args_renders_kwargs(agent_mod):
    out = agent_mod._brief_args((), {"source": "samples/x.csv", "apply": True})
    assert "source=samples/x.csv" in out and "apply=True" in out


def test_brief_args_renders_positional(agent_mod):
    assert agent_mod._brief_args(("a", "b"), {}) == "a, b"


def test_thinking_reads_the_assistant_text(agent_mod):
    class _Msg:
        content = "I should check the schema first."

    class _Resp:
        message = _Msg()

    assert "check the schema" in agent_mod._thinking(_Resp())


def test_thinking_handles_a_response_without_content(agent_mod):
    assert agent_mod._thinking(object()) == ""


def test_requested_tools_reads_tool_name(agent_mod):
    class _Call:
        tool_name = "run_import"

    class _Resp:
        tool_calls = [_Call()]

    assert agent_mod._requested_tools(_Resp()) == ["run_import"]


def test_requested_tools_reads_nested_tool_metadata(agent_mod):
    class _Meta:
        name = "create_field"

    class _Tool:
        metadata = _Meta()

    class _Call:
        tool = _Tool()

    class _Resp:
        tool_calls = [_Call()]

    assert agent_mod._requested_tools(_Resp()) == ["create_field"]


def test_requested_tools_falls_back_to_the_message(agent_mod):
    """Some response shapes only carry tool calls on the message."""
    class _Call:
        tool_name = "set_mapping"

    class _Msg:
        tool_calls = [_Call()]

    class _Resp:
        message = _Msg()

    assert agent_mod._requested_tools(_Resp()) == ["set_mapping"]


def test_requested_tools_is_empty_without_calls(agent_mod):
    assert agent_mod._requested_tools(object()) == []


def test_llm_call_is_announced_live(agent_mod, live, capsys):
    """The user must see the remote call start and finish, with its cost."""
    import asyncio
    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod._TRANSCRIPT_CTX.update({"round": 1, "log_event": lambda **e: None})
    asyncio.run(agent_mod.instrumented_llm(_FakeLLM)().achat(messages=["a", "b"]))
    out = capsys.readouterr().out
    assert "llm #1 → 2 msg(s)" in out
    assert "llm #1 ←" in out
    assert "120 tok" in out, "token usage must be visible live"


def test_live_shows_what_the_model_wants_to_do(agent_mod, live, capsys):
    import asyncio

    class _Call:
        tool_name = "run_import"

    class _Msg:
        content = "The import failed for every row."
        tool_calls = [_Call()]

    class _Resp:
        raw = _Raw()
        message = _Msg()

    class _LLM:
        model = "m"

        async def achat(self, *a, **k):
            return _Resp()

    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod._TRANSCRIPT_CTX.update({"round": 1, "log_event": lambda **e: None})
    asyncio.run(agent_mod.instrumented_llm(_LLM)().achat(messages=["a"]))
    out = capsys.readouterr().out
    assert "wants run_import" in out
    assert "The import failed for every row." in out, "thinking must be shown"


def test_tool_calls_carry_a_ToolUse_marker(agent_mod, live, capsys):
    """`ToolUse:<name>` is the greppable marker for every tool invocation."""
    def run_map(source, doctype=""):
        return "map exit 0"

    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod._wrap_tool(run_map, "run_map")(source="samples/x.csv", doctype="Customer")
    out = capsys.readouterr().out
    assert "ToolUse:run_map" in out
    assert "source=samples/x.csv" in out
    assert "ToolResult:run_map ✓ map exit 0" in out


def test_every_tool_use_is_marked_so_grep_finds_it(agent_mod, live, capsys):
    agent_mod._TRANSCRIPT_CTX.clear()
    for name in ("run_map", "get_record", "list_records"):
        agent_mod._wrap_tool(lambda **k: "ok", name)()
    out = capsys.readouterr().out
    marked = [ln for ln in out.splitlines() if "ToolUse:" in ln]
    assert len(marked) == 3
    assert [ln.split("ToolUse:")[1].split()[0] for ln in marked] == [
        "run_map", "get_record", "list_records"]


def test_failing_tool_calls_carry_a_ToolResult_marker(agent_mod, live, capsys):
    def boom(*a, **k):
        raise ValueError("nope")

    agent_mod._TRANSCRIPT_CTX.clear()
    with pytest.raises(ValueError):
        agent_mod._wrap_tool(boom, "boom")()
    out = capsys.readouterr().out
    assert "ToolUse:boom" in out
    assert "ToolResult:boom ✗ ValueError: nope" in out, \
        "a raising tool must be visible immediately"


def test_quiet_flag_wires_through(agent_mod):
    assert agent_mod.build_parser().parse_args([]).quiet is False
    args = agent_mod.build_parser().parse_args(["--quiet"])
    assert args.quiet is True


# ------------------------------------------- import failure digest
def _warnings(rows, err):
    return "\n".join(f"WARNING: row {r}: Customer 'C{r}' failed: {err}" for r in rows)


SCHEMA_ERR = ('HTTP 500 POST /api/resource/Customer: MySQLdb.OperationalError: '
              '(1054, "Unknown column \'lead_time_days\' in \'INSERT INTO\'")')


def test_warning_digest_is_empty_without_warnings(agent_mod):
    assert agent_mod.warning_digest("REST upsert: created 5, failed 0") == ""


def test_warning_digest_groups_identical_causes(agent_mod):
    """Without this the agent knows '29 failed' but not why, and guesses."""
    out = _warnings(range(2, 31), SCHEMA_ERR)
    digest = agent_mod.warning_digest(out)
    assert "x29" in digest, "the same cause across rows must collapse to one entry"
    assert "Unknown column 'lead_time_days'" in digest


def test_warning_digest_flags_a_single_shared_cause(agent_mod):
    digest = agent_mod.warning_digest(_warnings(range(2, 31), SCHEMA_ERR))
    assert "shares ONE reason" in digest
    assert "schema/environment" in digest


def test_warning_digest_does_not_flag_a_single_row(agent_mod):
    """One row failing is data, not a schema problem."""
    assert "shares ONE reason" not in agent_mod.warning_digest(
        _warnings([2], SCHEMA_ERR))


def test_warning_digest_ranks_by_frequency(agent_mod):
    out = "\n".join([
        _warnings([2], "boom A"),
        _warnings([3, 4, 5], "boom B"),
    ])
    digest = agent_mod.warning_digest(out)
    assert digest.index("boom B") < digest.index("boom A"), "most common cause first"
    assert "x3" in digest


def test_warning_digest_caps_the_listing(agent_mod):
    out = "\n".join(_warnings([i], f"distinct failure {i}") for i in range(2, 10))
    digest = agent_mod.warning_digest(out, cap=3)
    assert digest.count("distinct failure") == 3
    assert "+5 more distinct" in digest


def test_warning_digest_ignores_non_warning_lines(agent_mod):
    out = "\n".join(["Prepared 29 payloads", "REST upsert: created 0, failed 2",
                     _warnings([2, 3], "real cause")])
    digest = agent_mod.warning_digest(out)
    assert "real cause" in digest
    assert "Prepared" not in digest


def test_warning_digest_handles_warnings_without_the_failed_prefix(agent_mod):
    out = "WARNING: row 30: missing Customer Name (skipped)"
    digest = agent_mod.warning_digest(out)
    assert "missing Customer Name" in digest


def test_run_import_result_includes_the_digest(agent_mod, monkeypatch):
    """The tool's return value is what the model actually sees."""
    captured = {}

    def _fake_erpgen(cmd, timeout=300):
        captured["cmd"] = cmd
        # the reason sits at the FRONT, far outside a 2000-char tail
        return 0, _warnings(range(2, 31), SCHEMA_ERR) + "\nREST upsert: created 0, failed 29"

    monkeypatch.setattr(agent_tools, "_erpgen", _fake_erpgen)
    result = agent_mod.t_run_import("samples/customers.csv", "Customer", apply=True)
    assert "Unknown column 'lead_time_days'" in result, \
        "the failure reason must survive into the tool result"
    assert "shares ONE reason" in result


# ------------------------------------------------- LLM call logging
class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120


class _Raw:
    usage = _Usage()


class _ChatResponse:
    raw = _Raw()


class _FakeLLM:
    """Stands in for OpenAILike: only the two funnel methods exist."""

    model = "deepseek-v4-flash"

    def __init__(self, fail=False):
        self.fail = fail

    async def achat(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("upstream 500")
        return _ChatResponse()

    async def astream_chat(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("upstream 500")

        async def _gen():
            yield _ChatResponse()
            yield _ChatResponse()
        return _gen()


def _capture(agent_mod, monkeypatch, round_no=1):
    events: list = []
    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod._TRANSCRIPT_CTX.update(
        {"round": round_no, "log_event": lambda **e: events.append(e)})
    return events


def _names(events):
    return [e["event"] for e in events]


def test_every_llm_call_is_logged(agent_mod, monkeypatch):
    """The user-visible contract: one remote call => one logged request."""
    import asyncio
    events = _capture(agent_mod, monkeypatch)
    llm = agent_mod.instrumented_llm(_FakeLLM)()
    asyncio.run(llm.achat(messages=["a", "b"]))
    assert _names(events) == ["llm_request", "llm_response"]


def test_llm_request_records_round_method_model_and_size(agent_mod, monkeypatch):
    import asyncio
    events = _capture(agent_mod, monkeypatch, round_no=4)
    asyncio.run(agent_mod.instrumented_llm(_FakeLLM)().achat(messages=["a", "b"]))
    req = events[0]
    assert req["round"] == 4
    assert req["method"] == "achat"
    assert req["model"] == "deepseek-v4-flash"
    assert req["messages"] == 2


def test_llm_response_records_duration_and_tokens(agent_mod, monkeypatch):
    import asyncio
    events = _capture(agent_mod, monkeypatch)
    asyncio.run(agent_mod.instrumented_llm(_FakeLLM)().achat(messages=["a"]))
    resp = events[1]
    assert resp["duration_ms"] >= 0
    assert resp["total_tokens"] == 120
    assert resp["prompt_tokens"] == 100


def test_llm_failures_are_logged_and_still_raised(agent_mod, monkeypatch):
    """A failed call must be visible in the log, not just surface as a traceback."""
    import asyncio
    events = _capture(agent_mod, monkeypatch)
    llm = agent_mod.instrumented_llm(_FakeLLM)(fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(llm.achat(messages=["a"]))
    assert _names(events) == ["llm_request", "llm_failure"]
    assert "upstream 500" in events[1]["error"]


def test_streaming_call_is_logged_once_fully_drained(agent_mod, monkeypatch):
    import asyncio
    events = _capture(agent_mod, monkeypatch)

    async def _drain():
        gen = await agent_mod.instrumented_llm(_FakeLLM)().astream_chat(messages=["a"])
        async for _chunk in gen:
            pass

    asyncio.run(_drain())
    assert _names(events) == ["llm_request", "llm_response"]
    assert events[1]["streamed"] is True


def test_streaming_failure_is_logged(agent_mod, monkeypatch):
    import asyncio
    events = _capture(agent_mod, monkeypatch)

    async def _drain():
        gen = await agent_mod.instrumented_llm(_FakeLLM)(fail=True).astream_chat(
            messages=["a"])
        async for _chunk in gen:
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(_drain())
    assert _names(events) == ["llm_request", "llm_failure"]


def test_llm_calls_are_numbered_and_counted(agent_mod, monkeypatch):
    import asyncio
    events = _capture(agent_mod, monkeypatch)
    llm = agent_mod.instrumented_llm(_FakeLLM)()
    for _ in range(3):
        asyncio.run(llm.achat(messages=["a"]))
    assert [e["call_no"] for e in events if e["event"] == "llm_request"] == [1, 2, 3]
    assert agent_mod._TRANSCRIPT_CTX["llm_calls"] == 3


def test_logging_never_breaks_a_run_with_no_transcript(agent_mod):
    """Outside a run there is no transcript; logging must be a no-op.

    Asserted, not merely exercised: the wrapper has to come back with the model's
    response *and* leave nothing hooked for logging — as written before, this test
    would have passed just as happily if the logger had started writing to a file
    of its own.
    """
    import asyncio
    agent_mod._TRANSCRIPT_CTX.clear()

    response = asyncio.run(
        agent_mod.instrumented_llm(_FakeLLM)().achat(messages=["a"]))

    assert response is not None, "the wrapper still returns the model's response"
    assert agent_mod._TRANSCRIPT_CTX.get("log_event") is None, \
        "no transcript is open, so nothing may be hooked into it"


def test_prompt_content_is_not_logged(agent_mod, monkeypatch):
    """Migration data must not leak into the transcript."""
    import asyncio
    events = _capture(agent_mod, monkeypatch)
    secret = "SECRET-CUSTOMER-NAME"
    asyncio.run(agent_mod.instrumented_llm(_FakeLLM)().achat(messages=[secret]))
    assert secret not in json.dumps(events)


@pytest.mark.parametrize("raw,expected", [
    (_Raw(), {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}),
    (type("R", (), {"usage": None})(), {}),
    (None, {}),
    ({"usage": {"total_tokens": 5}}, {"total_tokens": 5}),
])
def test_llm_usage_is_tolerant(agent_mod, raw, expected):
    class _Resp:
        pass

    r = _Resp()
    r.raw = raw
    assert agent_mod.llm_usage(r) == expected


def test_llm_usage_handles_a_response_without_raw(agent_mod):
    assert agent_mod.llm_usage(object()) == {}


# ------------------------------------------------------ stall escape hatch
STUCK = [{"kind": "link_value_conflict", "source": "Link Name (Links)",
          "target": "links.link_name", "doctype": "link_doctype",
          "severity": "error"}]
OTHER = [{"kind": "required_missing", "field": "customer_name",
          "severity": "error"}]


def test_fingerprint_is_stable_for_the_same_conflicts(agent_mod):
    assert (agent_mod._stall_fingerprint(STUCK)
            == agent_mod._stall_fingerprint(list(STUCK)))


def test_fingerprint_changes_when_the_conflicts_change(agent_mod):
    """A flat count is not enough — different conflicts are still progress."""
    assert agent_mod._stall_fingerprint(STUCK) != agent_mod._stall_fingerprint(OTHER)


def test_stall_counter_starts_at_zero_then_counts_identical_rounds(agent_mod):
    advance = agent_mod._advance_stall
    fp = agent_mod._stall_fingerprint(STUCK)
    stalled = advance(fp, None, 0)          # round 1: nothing to compare against
    assert stalled == 0
    stalled = advance(fp, fp, stalled)      # round 2: unchanged
    assert stalled == 1
    stalled = advance(fp, fp, stalled)      # round 3: unchanged again
    assert stalled == 2


def test_stall_counter_resets_when_anything_changes(agent_mod):
    fp = agent_mod._stall_fingerprint(STUCK)
    other = agent_mod._stall_fingerprint(OTHER)
    assert agent_mod._advance_stall(other, fp, 2) == 0, "a different conflict is progress"


def test_stall_counter_resets_when_conflicts_are_resolved(agent_mod):
    fp = agent_mod._stall_fingerprint(STUCK)
    assert agent_mod._advance_stall(frozenset(), fp, 2) == 0, "empty means converged"


def test_escape_hatch_defaults_to_two_rounds(agent_mod):
    assert agent_mod.build_parser().parse_args([]).max_stall_rounds == 2


def test_escape_hatch_can_be_disabled(agent_mod):
    args = agent_mod.build_parser().parse_args(["--max-stall-rounds", "0"])
    assert args.max_stall_rounds == 0


STUCK_ANALYSIS = {
    "base_url": "http://localhost:8082",
    "conflicts": [{"kind": "link_value_conflict", "source": "Link Name (Links)",
                   "target": "links.link_name", "doctype": "link_doctype",
                   "severity": "error"}],
}


class _Workflow:
    async def run(self, **kw):
        class _R:
            response = "I tried to fix it."
        return _R()


class _Recorder:
    """Stand-in for Transcript that keeps the events instead of writing them."""

    def __init__(self):
        self.path = "/tmp/agent-test.jsonl"
        self.events: list = []

    def log(self, **entry):
        self.events.append(entry)


class _LoopArgs:
    max_rounds = 20
    max_stall_rounds = 2
    max_iterations = 50
    api_base = "http://localhost:8082"
    model = ""
    provider = "deepseek"
    run = None


def _drive_loop(agent_mod, monkeypatch, args, analysis=None):
    """Run the real `_run_rounds` with a stubbed LLM and a frozen analysis."""
    import asyncio

    async def _fake_round(workflow, msg, max_iterations=0):
        return "I tried to fix it."

    monkeypatch.setattr(agent_mod, "_run_agent_round", _fake_round)
    monkeypatch.setattr(agent_mod, "latest_analysis",
                        lambda doctype, source="": dict(analysis or STUCK_ANALYSIS))
    recorder = _Recorder()
    outcome = asyncio.run(agent_mod._run_rounds(
        args, _Workflow(), recorder, dict(analysis or STUCK_ANALYSIS),
        "Contact", "samples/contacts.csv"))
    return outcome, recorder


def test_round_verification_reads_the_source_scoped_analysis(agent_mod, monkeypatch):
    """The post-round re-read must pass the source, or several worksheets for one
    doctype make `latest_analysis` error into None and the loop keeps a stale
    analysis — the false stall this run hit (Customer: e2e + dirty)."""
    import asyncio

    seen = {}

    async def _fake_round(workflow, msg, max_iterations=0):
        return "done"

    def _fresh(doctype, source=""):
        seen["args"] = (doctype, source)
        # the round actually cleared the conflicts; a source-scoped re-read
        # reflects that, so the loop converges instead of stalling
        return {"base_url": "u", "conflicts": []}

    monkeypatch.setattr(agent_mod, "_run_agent_round", _fake_round)
    monkeypatch.setattr(agent_mod, "latest_analysis", _fresh)

    outcome = asyncio.run(agent_mod._run_rounds(
        _LoopArgs(), object(), _Recorder(), dict(STUCK_ANALYSIS),
        "Customer", "samples/customers_e2e.csv"))

    assert seen["args"] == ("Customer", "samples/customers_e2e.csv")
    assert outcome.converged is True


def test_escape_hatch_fires_when_conflicts_never_change(agent_mod, monkeypatch):
    """The whole point: stop burning rounds instead of grinding to --max-rounds."""
    outcome, recorder = _drive_loop(agent_mod, monkeypatch, _LoopArgs())
    assert outcome.exit_code == 4, "must bail with the 'stalled' exit code"
    assert outcome.converged is False
    assert any(e.get("event") == "stalled" for e in recorder.events), \
        "the stall must be recorded in the transcript"


def test_escape_hatch_reports_the_stuck_conflicts(agent_mod, monkeypatch, capsys):
    _drive_loop(agent_mod, monkeypatch, _LoopArgs())
    err = capsys.readouterr().err
    assert "STALLED" in err
    assert "link_doctype" in err, "the suspect doctype must be named"


def test_escape_hatch_waits_for_the_configured_number_of_rounds(agent_mod, monkeypatch):
    """Default 2 => it should give up during round 3, not round 1."""
    calls = {"n": 0}

    async def _fake_round(workflow, msg, max_iterations=0):
        return "tried"

    def _fresh(doctype, source=""):
        calls["n"] += 1
        return dict(STUCK_ANALYSIS)

    monkeypatch.setattr(agent_mod, "_run_agent_round", _fake_round)
    monkeypatch.setattr(agent_mod, "latest_analysis", _fresh)
    import asyncio
    outcome = asyncio.run(agent_mod._run_rounds(
        _LoopArgs(), _Workflow(), _Recorder(), dict(STUCK_ANALYSIS),
        "Contact", "samples/contacts.csv"))
    assert outcome.exit_code == 4
    assert calls["n"] == 3, "round 1 sets the baseline; 2 & 3 are the stall"


def test_escape_hatch_disabled_runs_to_the_round_cap(agent_mod, monkeypatch):
    class _NoHatch(_LoopArgs):
        max_rounds = 3
        max_stall_rounds = 0

    outcome, recorder = _drive_loop(agent_mod, monkeypatch, _NoHatch())
    assert outcome.exit_code == 0, "0 disables the check"
    assert not any(e.get("event") == "stalled" for e in recorder.events)


def test_loop_converges_when_conflicts_clear(agent_mod, monkeypatch):
    """Sanity check on the same harness: no conflicts => converged, no stall."""
    clean = {"base_url": "http://localhost:8082", "conflicts": []}
    outcome, _ = _drive_loop(agent_mod, monkeypatch, _LoopArgs(), analysis=clean)
    assert outcome.converged is True
    assert outcome.exit_code == 0


def test_conflict_description_names_the_suspect_doctype(agent_mod):
    """The stall report must point at 'link_doctype' — that is the giveaway."""
    line = agent_mod._describe_conflict(STUCK[0])
    assert "link_value_conflict" in line
    assert "Link Name (Links)" in line
    assert "link_doctype" in line


def test_stall_report_mentions_how_to_escape(agent_mod, capsys):
    class _Args:
        max_rounds = 20

    agent_mod._report_stall(STUCK, 2, "/tmp/agent-x.jsonl", _Args())
    err = capsys.readouterr().err
    assert "STALLED" in err
    assert "link_doctype" in err
    assert "--max-stall-rounds 0" in err, "must say how to disable the check"
    assert "/tmp/agent-x.jsonl" in err, "must point at the transcript"


# ------------------------------------------------- describe_llm_error
class _ApiError(Exception):
    def __init__(self, message="boom", status_code=None):
        super().__init__(message)
        self.status_code = status_code


# names mirror the openai SDK so name-based classification is exercised
class APIConnectionError(_ApiError): pass


class AuthenticationError(_ApiError): pass


class PermissionDeniedError(_ApiError): pass


class NotFoundError(_ApiError): pass


class RateLimitError(_ApiError): pass


class BadRequestError(_ApiError): pass


class InternalServerError(_ApiError): pass


BASE = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"


def _describe(agent_mod, exc, **kw):
    return agent_mod.describe_llm_error(exc, BASE, MODEL, "deepseek", **kw)


def test_connection_error_explains_the_sdk_conflates_transport_failures(agent_mod):
    out = _describe(agent_mod, APIConnectionError("Connection error."))
    assert "Network/transport failure" in out
    assert "grep -iE 'proxy'" in out, "must suggest checking proxy vars"
    assert "curl -sS" in out and BASE in out, "must give a copy-pasteable probe"


def test_connection_error_is_recognized_by_status_too(agent_mod):
    """A generic exception carrying a status code must still classify."""
    out = _describe(agent_mod, _ApiError("gateway blew up", status_code=502))
    assert "server-side" in out and "502" in out


@pytest.mark.parametrize("exc,fragment", [
    (AuthenticationError("bad key", status_code=401), "API key"),
    (AuthenticationError("Incorrect API key provided"), "API key"),
    (PermissionDeniedError("nope", status_code=403), "not allowed"),
    (NotFoundError("no model", status_code=404), "Model in use"),
    (RateLimitError("slow down", status_code=429), "Rate limited"),
    (BadRequestError("too long", status_code=400), "context length"),
])
def test_status_specific_guidance(agent_mod, exc, fragment):
    assert fragment in _describe(agent_mod, exc)


def test_404_by_name_without_status_still_finds_the_model_hint(agent_mod):
    out = _describe(agent_mod, NotFoundError("model does not exist"))
    assert "Model in use: " + MODEL in out


def test_unknown_failure_points_at_the_debug_flag(agent_mod):
    out = _describe(agent_mod, ValueError("something odd"))
    assert "AGENT_DEBUG=1" in out


def test_help_block_always_names_endpoint_and_model(agent_mod):
    out = _describe(agent_mod, RateLimitError("x", status_code=429))
    assert f"endpoint : {BASE}" in out
    assert f"model    : {MODEL}" in out
    assert "provider : deepseek" in out


def test_help_block_offers_the_offline_path(agent_mod):
    out = _describe(agent_mod, AuthenticationError("x", status_code=401))
    assert "DOCTOR=1 scripts/run-all-agentic.sh" in out


def test_model_defaults_when_not_supplied(agent_mod):
    out = agent_mod.describe_llm_error(RateLimitError("x", status_code=429), BASE)
    assert "(provider default)" in out


def test_the_deepseek_default_is_the_canonical_v41_flash_id(agent_mod):
    """`deepseek-v4.1-flash` is rejected by the API; the id is `deepseek-flash`."""
    assert P.DEFAULT_DEEPSEEK_MODEL == "deepseek-flash"


# ----------------------------------------------- --model flash|pro (DeepSeek)
@pytest.mark.parametrize("given,expected", [
    ("flash", "deepseek-flash"),
    ("pro", "deepseek-v4-pro"),
    ("FLASH", "deepseek-flash"),        # case-insensitive
    ("  pro  ", "deepseek-v4-pro"),     # surrounding whitespace ignored
    ("", "deepseek-flash"),             # omitted -> provider default
    (None, "deepseek-flash"),
    ("deepseek-v4-pro", "deepseek-v4-pro"),   # a full id is never second-guessed
    ("gpt-4o-mini", "gpt-4o-mini"),
])
def test_deepseek_model_shorthands_resolve(agent_mod, given, expected):
    assert P.resolve_deepseek_model(given) == expected


def test_the_flash_alias_tracks_the_deepseek_default(agent_mod):
    """DeepSeek's default IS the flash tier, so the two cannot drift."""
    assert P.DEEPSEEK_MODEL_ALIASES["flash"] == \
        P.DEFAULT_DEEPSEEK_MODEL


def test_deepinfra_has_no_model_shorthands(agent_mod):
    """DeepInfra ids are used in full; there is no alias table to drift."""
    assert not hasattr(agent_mod, "DEEPINFRA_MODEL_ALIASES")
    assert P.DEFAULT_DEEPINFRA_MODEL == "zai-org/GLM-5.3"


def test_vetted_deepinfra_ids_are_full_names(agent_mod):
    """Every vetted id is namespaced, and the default is among them."""
    known = agent_mod.DEEPINFRA_KNOWN_MODELS
    assert known, "the vetted list must not be empty"
    assert all("/" in mid for mid in known), "ids must keep their namespace"
    assert P.DEFAULT_DEEPINFRA_MODEL in known
    for mid in ("Qwen/Qwen3.8-Flash", "Qwen/Qwen3.8-27B"):
        assert mid in known, f"{mid} is vetted and must be listed"


def test_the_vetted_list_is_not_an_allow_list(agent_mod, monkeypatch):
    """A provider id we never vetted still reaches the client unrewritten."""
    pytest.importorskip("llama_index.llms.openai_like")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "dummy-key-no-network")
    unvetted = "Qwen/Qwen3.5-397B-A17B"
    assert unvetted not in agent_mod.DEEPINFRA_KNOWN_MODELS
    assert P._deepinfra_llm(unvetted, "").model == unvetted


def test_doctor_lists_the_vetted_deepinfra_models(agent_mod, capsys, monkeypatch):
    monkeypatch.setattr(agent_mod, "resolve_doctype", lambda args: ("Supplier", False))
    monkeypatch.setattr(agent_mod, "latest_analysis", lambda dt: None)
    agent_mod.cmd_doctor(_run_args())
    out = capsys.readouterr().out
    assert "Qwen/Qwen3.8-Flash" in out
    assert "Qwen/Qwen3.8-27B" in out
    assert "any other id also works" in out, "must not read as an allow-list"


def test_deepseek_llm_uses_the_resolved_model(agent_mod, monkeypatch):
    pytest.importorskip("llama_index.llms.openai_like")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-key-no-network")
    assert P._deepseek_llm("pro", "").model == "deepseek-v4-pro"
    assert P._deepseek_llm("flash", "").model == "deepseek-flash"
    assert P._deepseek_llm("", "").model == "deepseek-flash"


def test_deepinfra_llm_uses_the_full_model_name(agent_mod, monkeypatch):
    pytest.importorskip("llama_index.llms.openai_like")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "dummy-key-no-network")
    # the full namespaced id reaches the client verbatim, never rewritten
    assert P._deepinfra_llm("zai-org/GLM-5.3-Flash", "").model == \
        "zai-org/GLM-5.3-Flash"
    assert P._deepinfra_llm("zai-org/GLM-5.3", "").model == "zai-org/GLM-5.3"
    assert P._deepinfra_llm("zai-org/GLM-5.2", "").model == "zai-org/GLM-5.2"
    # omitted -> the flagship default, also spelled in full
    assert P._deepinfra_llm("", "").model == "zai-org/GLM-5.3"
    assert P._deepinfra_llm("   ", "").model == "zai-org/GLM-5.3"


# ------------------------------------------------------- provider resolution
@pytest.fixture
def no_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DEEPINFRA_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_effective_provider_honours_an_explicit_choice(no_keys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")   # what auto-detect would pick
    assert P.effective_provider("deepinfra") == "deepinfra"


def test_effective_provider_resolves_auto_in_preference_order(no_keys, monkeypatch):
    assert P.effective_provider("auto") == "", "no key means no provider"
    monkeypatch.setenv("DEEPINFRA_API_KEY", "x")
    assert P.effective_provider("auto") == "deepinfra"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert P.effective_provider("auto") == "deepseek"
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert P.effective_provider("auto") == "openai"


def test_effective_provider_rejects_an_unknown_name(no_keys, monkeypatch):
    """A typo must not silently fall through to auto-detection."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert P.effective_provider("deepsek") == ""


def test_api_base_for_prefers_the_flag_then_the_provider(no_keys, monkeypatch):
    assert P.api_base_for("deepinfra") == "https://api.deepinfra.com/v1/openai"
    assert P.api_base_for("openai") == "https://api.openai.com/v1"
    assert P.api_base_for("deepinfra", "http://localhost:9/v1") == \
        "http://localhost:9/v1"
    # auto must follow the key that actually won, not assume DeepSeek
    monkeypatch.setenv("DEEPINFRA_API_KEY", "x")
    assert P.api_base_for("auto") == "https://api.deepinfra.com/v1/openai"


def test_get_llm_names_the_key_that_is_missing(no_keys):
    with pytest.raises(SystemExit, match="DEEPINFRA_API_KEY is not set"):
        P.get_llm("deepinfra", "")


def test_get_llm_explains_when_nothing_is_configured(no_keys):
    with pytest.raises(SystemExit, match="No LLM provider configured"):
        P.get_llm("auto", "")


def test_get_llm_builds_the_auto_detected_provider(no_keys, monkeypatch):
    pytest.importorskip("llama_index.llms.openai_like")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "dummy-key-no-network")
    llm = P.get_llm("auto", "")
    assert llm.model == P.DEFAULT_DEEPINFRA_MODEL
    assert llm.api_base == "https://api.deepinfra.com/v1/openai"


# ------------------------------------------------------------ _is_llm_error
def test_is_llm_error_for_sdk_and_transport_types(agent_mod):
    assert agent_mod._is_llm_error(APIConnectionError("x")) is True
    assert agent_mod._is_llm_error(RateLimitError("x")) is True
    assert agent_mod._is_llm_error(TimeoutError("x")) is True


def test_is_llm_error_rejects_our_own_bugs(agent_mod):
    """A KeyError in our own code must NOT be dressed up as a network problem."""
    assert agent_mod._is_llm_error(ValueError("bug")) is False
    assert agent_mod._is_llm_error(KeyError("bug")) is False


def test_is_llm_error_detects_the_real_sdk_module(agent_mod):
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    exc = openai.APIConnectionError(request=httpx.Request("POST", BASE))
    assert agent_mod._is_llm_error(exc) is True
    assert "Network/transport failure" in _describe(agent_mod, exc)


# ------------------------------- the agent's import joins its own run context
def test_run_import_propagates_the_run_id_as_a_global_flag(agent_mod, monkeypatch):
    """Without --run the import journals separately and revert misses its rows."""
    seen = {}

    def _fake(cmd, timeout=300):
        seen["cmd"] = cmd
        return 0, "ok"

    monkeypatch.setattr(agent_tools, "_erpgen", _fake)
    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod._TRANSCRIPT_CTX["run"] = "r1"
    agent_mod.t_run_import("samples/x.csv", "Customer", apply=True)
    cmd = seen["cmd"]
    assert cmd[:2] == ["--run", "r1"], "--run is global: it must precede 'import'"
    assert cmd[2] == "import" and "--apply" in cmd


def test_run_import_omits_the_flag_without_a_run_id(agent_mod, monkeypatch):
    seen = {}

    def _fake(cmd, timeout=300):
        seen["cmd"] = cmd
        return 0, "ok"

    monkeypatch.setattr(agent_tools, "_erpgen", _fake)
    agent_mod._TRANSCRIPT_CTX.clear()
    agent_mod.t_run_import("samples/x.csv", "Customer", apply=True)
    assert "--run" not in seen["cmd"]


def test_round_loop_publishes_the_run_id_for_tools(agent_mod, monkeypatch):
    """The tool wrapper reads it from _TRANSCRIPT_CTX, so the loop must set it."""
    import asyncio
    captured = {}

    async def _fake_round(workflow, msg, max_iterations=0):
        captured["run"] = agent_mod._TRANSCRIPT_CTX.get("run")
        return "done"

    monkeypatch.setattr(agent_mod, "_run_agent_round", _fake_round)
    monkeypatch.setattr(agent_mod, "latest_analysis",
                        lambda dt, source="": {"base_url": "u", "conflicts": []})
    asyncio.run(agent_mod._run_rounds(_run_args(run="myrun"), object(), _T(),
                                      {"base_url": "u", "conflicts": []},
                                      "Item", "s.csv"))
    assert captured["run"] == "myrun"
