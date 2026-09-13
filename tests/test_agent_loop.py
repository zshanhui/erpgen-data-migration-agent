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


# ------------------------------------------------------------------- llm base
def test_llm_base_defaults_to_deepseek(agent_mod):
    assert agent_mod._llm_base(_args()) == "https://api.deepseek.com"
    assert agent_mod._llm_base(_args(api_base="http://127.0.0.1:8765/v1")) == \
        "http://127.0.0.1:8765/v1"


def test_preflight_is_skipped_for_openai(agent_mod, monkeypatch):
    """OpenAI keys have no DeepSeek preflight; otherwise every run would probe."""
    called = []
    monkeypatch.setattr(agent_mod, "llm_preflight",
                        lambda *a, **k: called.append(a) or (True, ""))
    assert agent_mod._preflight_llm(_args(provider="openai")) is True
    assert called == []
