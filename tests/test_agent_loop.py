"""P0: the agent's run loop, decomposed.

`run_agent` used to be one 192-line function (37 branches) mixing preflight,
analysis loading, transcript/journal setup, the convergence loop and teardown.
It is now small named pieces — these tests pin the behaviour of each so the
decomposition stays honest.

Pure: no network, no LLM, no live site.
"""
from __future__ import annotations

import argparse
import json

from erpgen import llm_providers as P  # provider logic lives here now
from erpgen.agent import tools as agent_tools


# ------------------------------------------------------------------- helpers
def _args(**kw):
    base = dict(api_base=None, model=None, provider="deepseek", max_iterations=50,
                analysis=None, source=None, defaults=None, max_rounds=20, run=None,
                base="http://localhost:8082")
    base.update(kw)
    return argparse.Namespace(**base)


def _analysis(*severities, **extra):
    return {"doctype": "Item", "source": "samples/items.csv", "conflicts":
            [{"severity": s, "kind": f"k{i}", "source": f"col{i}"}
             for i, s in enumerate(severities)], **extra}


# ----------------------------------------------------------------- transcript
def test_transcript_writes_jsonl_with_timestamps(agent_mod):
    t = agent_mod.Transcript.open("Item")
    try:
        t.log(event="probe", n=1)
        t.log(event="probe2")
        rows = [json.loads(line) for line in t.path.read_text().splitlines()]
    finally:
        t.close()
    assert [r["event"] for r in rows] == ["probe", "probe2"]
    assert all("ts" in r for r in rows)


def test_transcript_path_is_named_for_the_doctype(agent_mod):
    t = agent_mod.Transcript.open("Sales Order")
    try:
        assert t.path.name.startswith("agent-sales-order-")
        assert t.path.suffix == ".jsonl"
        assert t.logs_dir.name == "logs"
    finally:
        t.close()
        t.path.unlink()


def test_transcript_creates_the_log_directory(agent_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "ROOT", tmp_path)
    t = agent_mod.Transcript.open("Item")
    t.close()
    assert (tmp_path / "logs").is_dir()
    assert t.path.exists()


# --------------------------------------------------------------- error filter
def test_error_conflicts_filters_by_severity(agent_mod):
    a = _analysis("error", "info", "warning", "error")
    assert len(agent_mod._error_conflicts(a)) == 2
    assert all(c["severity"] == "error" for c in agent_mod._error_conflicts(a))


def test_error_conflicts_on_a_clean_analysis(agent_mod):
    assert agent_mod._error_conflicts(_analysis("info", "warning")) == []


# --------------------------------------------------------------- round prompt
def test_first_round_gets_the_whole_analysis(agent_mod):
    a = _analysis("error", "info", base_url="http://x")
    msg = agent_mod._round_message(1, "Item", "samples/items.csv", a)
    assert "Current analysis" in msg
    assert "samples/items.csv" in msg and "http://x" in msg
    assert '"info"' in msg, "round 1 shows everything, not just errors"


def test_later_rounds_get_only_what_still_fails(agent_mod):
    a = _analysis("error", "info")
    msg = agent_mod._round_message(2, "Item", "samples/items.csv", a)
    assert "REMAIN" in msg
    assert '"error"' in msg
    assert '"info"' not in msg, "later rounds should not re-send resolved noise"
    assert "run_map" in msg


# ----------------------------------------------------------- round error path
class WorkflowRuntimeError(Exception):
    pass


class APIConnectionError(Exception):
    pass


def test_iteration_exhaustion_bails_with_a_budget_hint(agent_mod, capsys):
    t = agent_mod.Transcript.open("Item")
    try:
        code = agent_mod._report_round_error(
            WorkflowRuntimeError("Max iterations of 20 reached!"), _args(), 1, t)
        events = [json.loads(l)["event"] for l in t.path.read_text().splitlines()]
    finally:
        t.close()
        t.path.unlink()
    assert code == 3
    assert events == ["iteration_exhausted"]
    err = capsys.readouterr().err
    assert "--max-iterations 100" in err, "must suggest a concrete larger budget"
    assert str(t.path) in err, "must point at the transcript to inspect"


def test_llm_failure_bails_with_the_help_block(agent_mod, capsys):
    t = agent_mod.Transcript.open("Item")
    try:
        code = agent_mod._report_round_error(APIConnectionError("Connection error."),
                                             _args(), 1, t)
        events = [json.loads(l)["event"] for l in t.path.read_text().splitlines()]
    finally:
        t.close()
        t.path.unlink()
    assert code == 2
    assert events == ["llm_error"]
    assert "Network/transport failure" in capsys.readouterr().err


def test_our_own_bug_is_not_disguised_as_an_llm_error(agent_mod, capsys):
    t = agent_mod.Transcript.open("Item")
    try:
        code = agent_mod._report_round_error(ValueError("a real bug"), _args(), 1, t)
        events = t.path.read_text().splitlines()
    finally:
        t.close()
        t.path.unlink()
    assert code is None, "must re-raise rather than bail"
    assert events == [], "nothing logged for a bug in our own code"
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------- analysis load
def test_load_analysis_reads_an_explicit_file(agent_mod, tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps(_analysis("error")), encoding="utf-8")
    got = agent_mod._load_analysis(_args(analysis=str(p)), "Item", False)
    assert got["doctype"] == "Item"


def test_load_analysis_errors_when_there_is_nothing_to_work_from(agent_mod, capsys):
    assert agent_mod._load_analysis(_args(), None, False) is None
    assert "no analysis found" in capsys.readouterr().err


def test_load_analysis_without_source_uses_the_newest_artifact(agent_mod, monkeypatch):
    sentinel = _analysis("error")
    monkeypatch.setattr(agent_mod, "latest_analysis", lambda dt: sentinel)
    assert agent_mod._load_analysis(_args(), "Item", False) is sentinel


# ------------------------------------------------------------------ outcome
def test_run_outcome_defaults_to_success(agent_mod):
    o = agent_mod.RunOutcome()
    assert (o.converged, o.response, o.exit_code) == (False, "", 0)


def test_run_outcome_is_mutable(agent_mod):
    o = agent_mod.RunOutcome(converged=True, response="done")
    assert o.converged and o.response == "done"


# ------------------------------------------- endpoint (provider-aware)
def test_llm_base_follows_the_provider(agent_mod):
    """It used to hardcode DeepSeek's host for every provider."""
    assert P._llm_base(_args()) == "https://api.deepseek.com"
    assert P._llm_base(_args(provider="deepinfra")) == \
        "https://api.deepinfra.com/v1/openai"
    assert P._llm_base(_args(provider="openai")) == "https://api.openai.com/v1"
    # an explicit --api-base still wins over the provider's own
    assert P._llm_base(_args(api_base="http://127.0.0.1:8765/v1")) == \
        "http://127.0.0.1:8765/v1"


def test_preflight_is_skipped_for_openai(agent_mod, monkeypatch):
    """OpenAI keys have no preflight; otherwise every run would probe."""
    called = []
    monkeypatch.setattr(P, "llm_preflight",
                        lambda *a, **k: called.append(a) or (True, ""))
    assert P._preflight_llm(_args(provider="openai")) is True
    assert called == []


def test_preflight_probes_the_provider_that_will_actually_serve(monkeypatch):
    """The bug: a DeepInfra run was probed against DeepSeek's host and key."""
    seen = {}
    monkeypatch.setenv("DEEPINFRA_API_KEY", "di-key")
    monkeypatch.setattr(P, "llm_preflight",
                        lambda base, key, **k: seen.update(base=base, key=key)
                        or (True, "ok"))
    assert P._preflight_llm(_args(provider="deepinfra")) is True
    assert seen["base"] == "https://api.deepinfra.com/v1/openai"
    assert seen["key"] == "di-key", "must send the key that host expects"


# ------------------------------------------------------------ data-quality gate
def _conflict(kind, severity="error", status="open"):
    return {"kind": kind, "severity": severity, "status": status,
            "source": f"col-{kind}"}


def test_data_quality_blockers_return_only_open_stale_errors(agent_mod):
    a = {"conflicts": [
        _conflict("duplicate_row"),
        _conflict("missing_value"),
        _conflict("possible_duplicate_row", severity="warning"),  # warning → never a gate
        _conflict("link_value_conflict"),             # mapping, not data quality
        _conflict("missing_value", status="corrected"),
        _conflict("missing_value", status="waived"),
        _conflict("missing_value", status="resolved"),
    ]}
    blockers = agent_mod._data_quality_blockers(a)
    assert [(c["kind"], c["status"]) for c in blockers] == \
        [("duplicate_row", "open"), ("missing_value", "open")]


def test_data_quality_blockers_on_a_clean_analysis(agent_mod):
    a = {"conflicts": [_conflict("link_value_conflict"),
                       _conflict("missing_value", status="corrected")]}
    assert agent_mod._data_quality_blockers(a) == []


def test_run_agent_gates_on_data_quality_before_any_llm(agent_mod, monkeypatch):
    import asyncio

    monkeypatch.setattr(agent_mod, "resolve_doctype", lambda args: ("Item", False))
    monkeypatch.setattr(agent_mod, "_load_analysis",
                        lambda *a, **k: {"doctype": "Item",
                                         "source": "samples/items.csv",
                                         "conflicts": [_conflict("duplicate_row")]})
    llm_called = []
    monkeypatch.setattr(agent_mod, "get_llm",
                        lambda *a, **k: llm_called.append(1) or object())

    rc = asyncio.run(agent_mod.run_agent(_args(source="samples/items.csv")))

    assert rc == agent_mod.EXIT_DATA_QUALITY
    assert llm_called == [], "the LLM must not be touched while data quality is broken"


def test_run_agent_proceeds_when_data_quality_is_clean(agent_mod, monkeypatch):
    import asyncio

    monkeypatch.setattr(agent_mod, "resolve_doctype", lambda args: ("Item", False))
    monkeypatch.setattr(agent_mod, "_load_analysis",
                        lambda *a, **k: {"doctype": "Item",
                                         "source": "samples/items.csv",
                                         "conflicts": [_conflict("link_value_conflict")]})
    llm_called = []
    monkeypatch.setattr(agent_mod, "get_llm",
                        lambda *a, **k: llm_called.append(1) or object())
    monkeypatch.setattr(agent_mod, "_preflight_llm", lambda args: False)

    rc = asyncio.run(agent_mod.run_agent(_args(source="samples/items.csv")))

    assert rc != agent_mod.EXIT_DATA_QUALITY
    assert llm_called, "a clean data-quality pass must reach the LLM mapping flow"


# ------------------------------------------------- llm data-quality correction
def _dq_loop(agent_mod, monkeypatch, proposal, answer, fresh_conflicts):
    import asyncio
    from types import SimpleNamespace

    blockers = [{"kind": "missing_value", "severity": "error", "status": "open",
                 "source": "Customer Name", "target": "customer_name", "rows": [30]}]

    class _Resp:
        class _Msg:
            content = proposal
        message = _Msg()

    class _LLM:
        async def achat(self, messages):
            return _Resp()

    applied = []

    def _add(path, corr, created_by="human"):
        applied.append(corr)
        return {"id": "c1"}, True

    monkeypatch.setattr(agent_mod, "get_llm", lambda *a, **k: _LLM())
    monkeypatch.setattr(agent_mod, "_preflight_llm", lambda args: True)
    monkeypatch.setattr(agent_mod, "read_source",
                        lambda p: SimpleNamespace(n_rows=3, headers=["Customer Name"],
                                                 rows=[["A"], ["B"], [""]]))
    # add_correction is imported inside _llm_data_quality_loop, so patch the
    # module it comes from rather than the agent module
    import erpgen.corrections as _corr

    monkeypatch.setattr(_corr, "add_correction", _add)
    monkeypatch.setattr(agent_mod, "_correction_sink",
                        lambda *a, **k: SimpleNamespace(effect=lambda *a, **k: None,
                                                        close=lambda: None))
    monkeypatch.setattr(agent_mod, "_erpgen", lambda cmd, timeout=300: (0, ""))
    monkeypatch.setattr(agent_mod, "latest_analysis",
                        lambda dt, source="": {"conflicts": fresh_conflicts})
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    analysis = {"conflicts": blockers, "source": {"key_column": "Customer Name"}}
    code = asyncio.run(agent_mod._llm_data_quality_loop(
        _args(), analysis, "Customer", "samples/x.csv", False))
    return code, applied


def test_extract_json_handles_fences_and_prose(agent_mod):
    assert agent_mod._extract_json('{"action": "skip_row"}') == {"action": "skip_row"}
    assert agent_mod._extract_json('Sure, here you go:\n```json\n{"action": "merge_rows"}\n```') \
        == {"action": "merge_rows"}
    assert agent_mod._extract_json("no json here") is None


def test_llm_dq_loop_applies_an_approved_correction(agent_mod, monkeypatch):
    code, applied = _dq_loop(
        agent_mod, monkeypatch,
        proposal='{"action":"skip_row","at":{"row":30},"reason":"blank","conflict":"missing_value:Customer Name:customer_name"}',
        answer="y", fresh_conflicts=[])
    assert code == 0
    assert len(applied) == 1


def test_llm_dq_loop_skips_a_declined_correction_and_stops(agent_mod, monkeypatch):
    code, applied = _dq_loop(
        agent_mod, monkeypatch,
        proposal='{"action":"skip_row","at":{"row":30},"reason":"blank","conflict":"missing_value:Customer Name:customer_name"}',
        answer="n",
        fresh_conflicts=[{"kind": "missing_value", "severity": "error",
                          "status": "open", "source": "Customer Name"}])
    assert code == agent_mod.EXIT_DATA_QUALITY
    assert applied == []


def test_llm_dq_loop_is_a_noop_when_data_is_clean(agent_mod, monkeypatch):
    import asyncio

    monkeypatch.setattr(agent_mod, "get_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM must not run")))
    code = asyncio.run(agent_mod._llm_data_quality_loop(
        _args(), {"conflicts": [], "source": {"key_column": "Customer Name"}},
        "Customer", "samples/x.csv", False))
    assert code == 0


def test_describe_proposal_is_human_readable(agent_mod):
    from types import SimpleNamespace

    src = SimpleNamespace(
        headers=["Customer Name", "City"],
        rows=[["Acme", "Cleveland"], ["Hollow Core Drilling", ""]], n_rows=2)
    kc = "Customer Name"

    assert agent_mod._describe_proposal(
        {"action": "skip_row", "at": {"row": 3}, "reason": "blank city"}, src, kc
    ) == "drop row 3 (Hollow Core Drilling) — blank city"
    assert agent_mod._describe_proposal(
        {"action": "set_value", "at": {"row": 3}, "column": "City", "value": "Cleveland"}, src, kc
    ) == "set City on row 3 (Hollow Core Drilling) to 'Cleveland'"
    assert agent_mod._describe_proposal(
        {"action": "merge_rows", "keep": {"row": 2}, "drop": [{"row": 3}]}, src, kc
    ) == "merge row 3 (Hollow Core Drilling) into row 2 (Acme)"
    assert agent_mod._describe_proposal(
        {"action": "dismiss_conflict", "conflict": "possible_duplicate_row:Customer Name",
         "reason": "two entities"}, src, kc
    ) == "waive possible_duplicate_row:Customer Name — two entities"


# --------------------------------------------------------- correct tool kwarg
def test_correct_tool_merges_a_top_level_conflict_kwarg(agent_mod, monkeypatch):
    """The model sometimes passes `conflict` top-level; fail-soft, not a TypeError."""
    import json as _json

    seen = {}

    def _fake_erpgen(cmd, timeout=300):
        seen["cmd"] = cmd
        return 0, "correct exit 0:\ncorrection c1 added"

    monkeypatch.setattr(agent_tools, "_erpgen", _fake_erpgen)
    out = agent_mod.t_correct(
        "samples/customers.csv", "Customer",
        '{"action":"set_value","at":{"row":5},"column":"Country","value":"US"}',
        conflict="link_value_conflict:Country:country",
    )
    sent = _json.loads(seen["cmd"][seen["cmd"].index("--json") + 1])
    assert sent["conflict"] == "link_value_conflict:Country:country"
    assert "correct exit 0" in out


def test_correct_tool_does_not_override_an_existing_conflict(agent_mod, monkeypatch):
    import json as _json

    seen = {}

    def _fake_erpgen(cmd, timeout=300):
        seen["cmd"] = cmd
        return 0, "correct exit 0:\ncorrection c1 added"

    monkeypatch.setattr(agent_tools, "_erpgen", _fake_erpgen)
    agent_mod.t_correct(
        "samples/customers.csv", "Customer",
        '{"action":"set_value","at":{"row":5},"column":"Country","value":"US",'
        '"conflict":"missing_value:Country:country"}',
        conflict="link_value_conflict:Country:country",
    )
    sent = _json.loads(seen["cmd"][seen["cmd"].index("--json") + 1])
    assert sent["conflict"] == "missing_value:Country:country"
