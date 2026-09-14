"""P0: picking which log `revert`/`status --latest` acts on.

`latest_run` used to sort by *filename*, so an arbitrary run id ("ctx-demo-01")
beat a genuinely newer run ("agent-loop-03") and pointed `revert` at a stale,
already-reverted journal.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from conftest import delete_record_effect, write_journal
from erpgen.context import latest_run, resolve_run


def _run_file(path, run_id: str, doctype: str = "Item", effects: int = 1):
    write_journal(path, [delete_record_effect(doctype, f"{doctype}-{i}")
                         for i in range(effects)],
                  run_id=run_id, doctype=doctype)
    return path


def _set_mtime(path, ts: float):
    os.utime(path, (ts, ts))


# --------------------------------------------------------------- resolve_run
def test_resolve_run_by_id(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    f = _run_file(logs / "run-acme-01.jsonl", "acme-01")
    assert resolve_run("acme-01", logs) == f


def test_resolve_run_slugs_the_id(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    f = _run_file(logs / "run-my-run-01.jsonl", "my-run-01")
    assert resolve_run("My Run 01", logs) == f
    assert resolve_run("MY  RUN   01", logs) == f


def test_resolve_run_accepts_an_explicit_path(tmp_path):
    f = _run_file(tmp_path / "elsewhere.jsonl", "x")
    assert resolve_run(str(f), tmp_path / "logs") == f


def test_resolve_run_raises_for_an_unknown_id(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    with pytest.raises(FileNotFoundError) as e:
        resolve_run("ghost", logs)
    assert "no run file" in str(e.value)


# ---------------------------------------------------------------- latest_run
def test_latest_run_is_newest_by_mtime_not_filename(tmp_path):
    """Regression: run ids are arbitrary, so filename order is meaningless."""
    logs = tmp_path / "logs"
    logs.mkdir()
    older = _run_file(logs / "run-zzz-last-alphabetically.jsonl", "zzz")
    newer = _run_file(logs / "run-aaa-first-alphabetically.jsonl", "aaa")
    _set_mtime(older, time.time() - 500)
    _set_mtime(newer, time.time())

    assert latest_run("Item", logs) == newer


def test_latest_run_ignores_a_reverted_file(tmp_path):
    from erpgen.journal import mark_reverted

    logs = tmp_path / "logs"
    logs.mkdir()
    old = _run_file(logs / "run-old.jsonl", "old")
    new = _run_file(logs / "run-new.jsonl", "new")
    _set_mtime(old, time.time() - 500)
    _set_mtime(new, time.time())

    mark_reverted(new, "new", 1, "ok")
    assert latest_run("Item", logs) == old


def test_latest_run_ignores_a_partially_reverted_file(tmp_path):
    from erpgen.journal import mark_reverted

    logs = tmp_path / "logs"
    logs.mkdir()
    f = _run_file(logs / "run-a.jsonl", "a", effects=2)
    mark_reverted(f, "a", 1, "ok")  # only one of two effects reverted
    assert latest_run("Item", logs) == f


def test_latest_run_considers_per_command_journals(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    j = write_journal(logs / "journal-item-20260911-000000.jsonl",
                      [delete_record_effect("Item", "A")], doctype="Item")
    assert latest_run("Item", logs) == j


def test_latest_run_prefers_the_newer_of_run_and_journal(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    j = write_journal(logs / "journal-item-20260911-000000.jsonl",
                      [delete_record_effect("Item", "A")], doctype="Item")
    r = _run_file(logs / "run-abc.jsonl", "abc")
    _set_mtime(j, time.time() - 500)
    _set_mtime(r, time.time())
    assert latest_run("Item", logs) == r


def test_latest_run_matches_doctype_by_content(tmp_path):
    """A run id says nothing about the doctype, so the file body decides."""
    logs = tmp_path / "logs"
    logs.mkdir()
    item = _run_file(logs / "run-a.jsonl", "a", doctype="Item")
    customer = _run_file(logs / "run-b.jsonl", "b", doctype="Customer")
    _set_mtime(item, time.time() - 500)
    _set_mtime(customer, time.time())
    assert latest_run("Item", logs) == item
    assert latest_run("Customer", logs) == customer


def test_latest_run_returns_none_when_nothing_mentions_the_doctype(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _run_file(logs / "run-a.jsonl", "a", doctype="Item")
    assert latest_run("Sales Order", logs) is None


def test_latest_run_on_an_empty_directory(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    assert latest_run("Item", logs) is None


def test_latest_run_does_not_skip_a_failed_revert_marker(tmp_path):
    from erpgen.journal import mark_reverted

    logs = tmp_path / "logs"
    logs.mkdir()
    old = _run_file(logs / "run-old.jsonl", "old")
    new = _run_file(logs / "run-new.jsonl", "new")
    _set_mtime(old, time.time() - 500)
    _set_mtime(new, time.time())

    mark_reverted(new, "new", 1, "failed")   # revert did NOT succeed
    assert latest_run("Item", logs) == new, "skipped a journal that still needs reverting"


def test_latest_run_falls_back_when_everything_was_reverted(tmp_path):
    """The loop prefers a non-reverted file but still resolves *something*."""
    from erpgen.journal import mark_reverted

    logs = tmp_path / "logs"
    logs.mkdir()
    only = _run_file(logs / "run-only.jsonl", "only")
    mark_reverted(only, "only", 1, "ok")
    assert latest_run("Item", logs) == only


# ------------------------------------------- preferring logs with something to undo
def test_latest_run_prefers_the_newest_log_that_has_effects(tmp_path):
    """`revert` needs work to undo: a log that changed nothing must not shadow an
    older one that did."""
    logs = tmp_path / "logs"
    logs.mkdir()
    with_effects = _run_file(logs / "run-older.jsonl", "older", effects=2)
    empty = write_journal(logs / "journal-item-20260914-000001.jsonl", [],
                          doctype="Item")           # run_start only
    _set_mtime(with_effects, time.time() - 500)
    _set_mtime(empty, time.time())

    # without the flag the newest still wins (that is `status --latest`)
    assert latest_run("Item", logs) == empty
    # with it, the empty log is passed over
    assert latest_run("Item", logs, require_effects=True) == with_effects


def test_latest_run_falls_back_to_the_newest_when_nothing_has_effects(tmp_path):
    """Better a resolvable path than none: the caller can still say what it found,
    and this keeps legacy empty journals from breaking the lookup."""
    logs = tmp_path / "logs"
    logs.mkdir()
    older = write_journal(logs / "journal-item-20260914-000001.jsonl", [],
                          doctype="Item")
    newer = write_journal(logs / "journal-item-20260914-000002.jsonl", [],
                          doctype="Item")
    _set_mtime(older, time.time() - 500)
    _set_mtime(newer, time.time())

    assert latest_run("Item", logs, require_effects=True) == newer


def test_latest_run_with_effects_still_skips_reverted_logs(tmp_path):
    from erpgen.journal import mark_reverted

    logs = tmp_path / "logs"
    logs.mkdir()
    older = _run_file(logs / "run-older.jsonl", "older", effects=1)
    newer = _run_file(logs / "run-newer.jsonl", "newer", effects=1)
    _set_mtime(older, time.time() - 500)
    _set_mtime(newer, time.time())
    mark_reverted(newer, "newer", 1, "ok")

    assert latest_run("Item", logs, require_effects=True) == older
