"""The agent's wiring: the tools it is handed, the journal each run opens, and
the summary it leaves behind.

`build_workflow`, `_open_journal`, `_correction_sink` and `_report_run_end` are
the connective tissue of a run: the registry becomes llama-index FunctionTools
(with the descriptions the model actually reads), every run opens exactly one
journal — its own, or the shared `--run` context — and the end of a run tells the
operator how to undo it. None of them ran in the suite, so an agent that silently
lost a tool, or an effect that missed the journal, would have looked green.
"""
from __future__ import annotations

import argparse
import warnings

import pytest

from erpgen import tools as erpgen_tools
from erpgen.agent import (SYSTEM_PROMPT, TOOLS, Transcript, _correction_sink,
                          _open_journal, _report_run_end, build_workflow)

CONFLICT = {
    "kind": "link_value_conflict",
    "severity": "error",
    "source": "Group",
    "target": "customer.customer_group",
    "doctype": "Customer Group",
    "missing_values": ["Raw Material"],
    "detail": "1 value(s) for 'Group' do not exist in Customer Group.",
}


@pytest.fixture(autouse=True)
def _restore_active_journal():
    """`_open_journal` publishes the journal globally; don't leak it into tests."""
    before = erpgen_tools.ACTIVE_JOURNAL
    yield
    erpgen_tools.ACTIVE_JOURNAL = before


def _args(**kw):
    base = dict(run=None, base="http://localhost:8082", log_dir="logs")
    base.update(kw)
    return argparse.Namespace(**base)


def _events():
    seen: list[dict] = []
    return seen, lambda **kw: seen.append(kw)


# ---------------------------------------------------------------- the workflow
def test_the_workflow_exposes_exactly_the_registered_tools():
    """The registry is what `--doctor` prints; the workflow is what the model
    gets. If they drift, the prompt describes tools the agent does not have."""
    llms = pytest.importorskip("llama_index.core.llms")

    with warnings.catch_warnings():
        # llama_index's pydantic models touch deprecated `__fields__` attrs while
        # being constructed; that is their internals, not our wiring, and four
        # warnings per run would hide a real deprecation of ours later
        warnings.simplefilter("ignore", DeprecationWarning)
        workflow = build_workflow(llms.MockLLM())
    agent = workflow.agents["migration_agent"]

    assert [t.metadata.name for t in agent.tools] == [t["name"] for t in TOOLS]
    assert [t.metadata.description for t in agent.tools] == \
        [t["description"] for t in TOOLS]
    assert agent.system_prompt == SYSTEM_PROMPT


def test_every_registered_tool_has_a_description_the_model_can_use():
    for tool in TOOLS:
        assert tool["name"] and tool["description"]
        assert callable(tool["fn"])
    assert len({t["name"] for t in TOOLS}) == len(TOOLS), "names must be unique"


# ----------------------------------------------------------------- the journal
def test_a_run_without_a_run_id_opens_its_own_journal(tmp_path):
    seen, log_event = _events()

    journal = _open_journal(_args(), "Item", "samples/items.csv", [], tmp_path,
                            log_event)

    assert type(journal).__name__ == "MigrationJournal"
    assert journal.path.parent == tmp_path
    assert erpgen_tools.ACTIVE_JOURNAL is journal, \
        "the tools journal through this global"
    assert [e["event"] for e in seen] == ["journal_open"]
    journal.close()


def test_a_run_with_a_run_id_joins_the_shared_context(tmp_path, capsys):
    seen, log_event = _events()

    journal = _open_journal(_args(run="myrun"), "Item", "samples/items.csv",
                            [CONFLICT], tmp_path, log_event)

    assert type(journal).__name__ == "MigrationContext"
    assert journal.run_id == "myrun"
    assert [r["kind"] for r in journal.pending_requirements()] == \
        ["link_value_conflict"], "the analysis' conflicts seed the context"
    assert [e["event"] for e in seen] == ["run_context_open"]
    assert seen[0]["requirements"] == 1
    assert "1 requirement(s) recorded" in capsys.readouterr().out
    journal.close()


def test_the_correction_sink_goes_where_the_run_goes(tmp_path):
    shared = _correction_sink(_args(run="myrun", log_dir=tmp_path), "Customer",
                              "samples/customers.csv")
    assert type(shared).__name__ == "MigrationContext"
    assert shared.run_id == "myrun"

    loose = _correction_sink(_args(log_dir=tmp_path), "Customer",
                             "samples/customers.csv")
    assert type(loose).__name__ == "MigrationJournal"
    assert loose.path.parent == tmp_path


# ------------------------------------------------------------------- run end
def test_the_end_of_a_run_reports_what_is_still_pending_and_how_to_undo_it(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("erpgen.agent.ROOT", tmp_path)
    transcript = Transcript.open("Item")
    journal = _open_journal(_args(run="myrun"), "Item", "samples/items.csv",
                            [CONFLICT], tmp_path, lambda **kw: None)

    _report_run_end(journal, transcript)

    out = capsys.readouterr().out
    assert "1 requirement(s) still pending" in out
    assert "PENDING" in out and "link_value_conflict" in out
    assert "revert this run: python3 erpgen.py revert myrun --apply" in out
    assert f"Agent transcript" in out and str(transcript.path) in out


def test_the_end_of_a_plain_journal_says_there_is_nothing_to_undo(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("erpgen.agent.ROOT", tmp_path)
    transcript = Transcript.open("Item")
    journal = _open_journal(_args(), "Item", "samples/items.csv", [], tmp_path,
                            lambda **kw: None)

    _report_run_end(journal, transcript)

    out = capsys.readouterr().out
    assert "Nothing to undo" in out
    assert "revert with: python3 erpgen.py revert" in out
