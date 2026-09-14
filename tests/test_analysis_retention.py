"""P0: analysis-artifact retention.

Every `map`/`import` writes a new timestamped analysis, so an agent run with
several rounds leaves a long trail (137 files had accumulated). Retention is
per doctype, and must never remove the artifact a running loop is reading.

Pure: writes only to tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from erpgen.analysis import KEEP_PER_DOCTYPE, prune_analyses, save_analysis


def _write(directory: Path, slug: str, stamp: str) -> Path:
    p = directory / f"analysis-{slug}-{stamp}.json"
    p.write_text("{}", encoding="utf-8")
    return p


def _stamps(n: int) -> list[str]:
    """n increasing, fixed-width stamps (lexical order == chronological)."""
    return [f"20260101-{i:012d}" for i in range(n)]


# ------------------------------------------------------------------ basics
def test_keeps_everything_when_under_the_cap(tmp_path):
    for stamp in _stamps(5):
        _write(tmp_path, "item", stamp)
    assert prune_analyses(tmp_path, keep=10) == []
    assert len(list(tmp_path.glob("analysis-*.json"))) == 5


def test_keeps_exactly_the_cap_and_removes_the_rest(tmp_path):
    for stamp in _stamps(10):
        _write(tmp_path, "item", stamp)
    removed = prune_analyses(tmp_path, keep=4)
    left = sorted(p.name for p in tmp_path.glob("analysis-*.json"))
    assert len(left) == 4
    assert len(removed) == 6


def test_keeps_the_newest_not_the_oldest(tmp_path):
    stamps = _stamps(6)
    for stamp in stamps:
        _write(tmp_path, "item", stamp)
    prune_analyses(tmp_path, keep=2)
    left = sorted(p.name for p in tmp_path.glob("analysis-*.json"))
    assert left == [f"analysis-item-{stamps[-2]}.json",
                    f"analysis-item-{stamps[-1]}.json"]


def test_never_removes_the_newest_for_a_doctype(tmp_path):
    """The convergence loop re-reads this file every round."""
    newest = _write(tmp_path, "item", _stamps(1)[0])
    for stamp in _stamps(30)[1:]:
        _write(tmp_path, "item", stamp)
    newest = tmp_path / f"analysis-item-{_stamps(30)[-1]}.json"
    prune_analyses(tmp_path, keep=5)
    assert newest.exists()
    assert len(list(tmp_path.glob("analysis-item-*.json"))) == 5


# -------------------------------------------------------------- per doctype
def test_cap_is_per_doctype_not_global(tmp_path):
    """A busy doctype must not evict a quiet one's history (or its newest)."""
    for stamp in _stamps(30):
        _write(tmp_path, "item", stamp)
    _write(tmp_path, "address", _stamps(1)[0])
    prune_analyses(tmp_path, keep=5)
    assert len(list(tmp_path.glob("analysis-item-*.json"))) == 5
    assert len(list(tmp_path.glob("analysis-address-*.json"))) == 1


def test_slugs_with_dashes_are_grouped_separately(tmp_path):
    # assert on exact names: a 'analysis-sales-*' glob would also match
    # 'analysis-sales-order-*', which hides a grouping bug
    stamps = _stamps(4)
    for stamp in stamps:
        _write(tmp_path, "sales-order", stamp)
        _write(tmp_path, "sales", stamp)
    prune_analyses(tmp_path, keep=2)
    remaining = sorted(p.name for p in tmp_path.glob("analysis-*.json"))
    expected = sorted(
        [f"analysis-sales-order-{s}.json" for s in stamps[-2:]]
        + [f"analysis-sales-{s}.json" for s in stamps[-2:]]
    )
    assert remaining == expected


def test_underscored_flow_slugs_are_kept_apart(tmp_path):
    for stamp in _stamps(4):
        _write(tmp_path, "customers_full", stamp)
        _write(tmp_path, "suppliers_full", stamp)
    prune_analyses(tmp_path, keep=1)
    assert len(list(tmp_path.glob("analysis-customers_full-*.json"))) == 1
    assert len(list(tmp_path.glob("analysis-suppliers_full-*.json"))) == 1


# ---------------------------------------------------------- safety / ignores
def test_unclassifiable_files_are_never_deleted(tmp_path):
    """Better to leave an unknown file than delete something we can't identify."""
    keep_me = tmp_path / "analysis-manual-notes.json"      # no stamp
    other = tmp_path / "plan.json"
    keep_me.write_text("{}", encoding="utf-8")
    other.write_text("{}", encoding="utf-8")
    for stamp in _stamps(6):
        _write(tmp_path, "item", stamp)
    prune_analyses(tmp_path, keep=1)
    assert keep_me.exists()
    assert other.exists()


def test_empty_directory_is_fine(tmp_path):
    assert prune_analyses(tmp_path, keep=25) == []


def test_missing_directory_is_fine(tmp_path):
    assert prune_analyses(tmp_path / "nope", keep=25) == []


# ------------------------------------------------------------- save triggers
def test_save_analysis_prunes_automatically(tmp_path):
    for i in range(KEEP_PER_DOCTYPE + 3):
        save_analysis({"doctype": "Item", "n": i}, tmp_path)
    left = list(tmp_path.glob("analysis-item-*.json"))
    assert len(left) == KEEP_PER_DOCTYPE


def test_save_analysis_keeps_its_own_new_file(tmp_path):
    for _ in range(KEEP_PER_DOCTYPE + 3):
        path = save_analysis({"doctype": "Item"}, tmp_path)
    assert path.exists(), "the artifact just returned must survive pruning"


def test_save_analysis_prunes_per_doctype(tmp_path):
    for _ in range(KEEP_PER_DOCTYPE + 3):
        save_analysis({"doctype": "Item"}, tmp_path)
    save_analysis({"doctype": "Address"}, tmp_path)
    assert len(list(tmp_path.glob("analysis-item-*.json"))) == KEEP_PER_DOCTYPE
    assert len(list(tmp_path.glob("analysis-address-*.json"))) == 1


def test_save_analysis_returns_a_named_artifact(tmp_path):
    path = save_analysis({"doctype": "Sales Order"}, tmp_path)
    assert path.name.startswith("analysis-sales-order-")
    assert path.read_text(encoding="utf-8").strip().startswith("{")


def test_default_keep_is_the_documented_cap():
    assert KEEP_PER_DOCTYPE == 10


# --------------------------------------------------------------- lookups
def test_paths_match_the_doctype_slug_exactly(tmp_path):
    """A prefix match is wrong: `analysis-customer-*` also matches Customer Group,
    and the longer slug sorts *after* the timestamp, so the wrong file wins."""
    from erpgen.analysis import analysis_paths

    _write(tmp_path, "customer", "20260914-100000000001")
    _write(tmp_path, "customer-group", "20260914-100000000002")
    _write(tmp_path, "item", "20260914-100000000003")

    paths = analysis_paths("Customer", tmp_path)
    assert [p.name for p in paths] == ["analysis-customer-20260914-100000000001.json"]


def test_paths_of_a_multi_word_doctype(tmp_path):
    from erpgen.analysis import analysis_paths

    _write(tmp_path, "customer-group", "20260914-100000000001")
    _write(tmp_path, "customer", "20260914-100000000002")
    assert [p.name for p in analysis_paths("Customer Group", tmp_path)] == [
        "analysis-customer-group-20260914-100000000001.json"]


def test_paths_ignore_files_that_do_not_match_the_convention(tmp_path):
    from erpgen.analysis import analysis_paths

    _write(tmp_path, "customer", "20260914-100000000001")
    (tmp_path / "analysis-customer-notes.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run-customer-20260914.jsonl").write_text("", encoding="utf-8")
    assert len(analysis_paths("Customer", tmp_path)) == 1


def test_latest_analysis_resolves_the_right_doctype(tmp_path, monkeypatch):
    from erpgen import agent

    d = tmp_path / "analysis"        # latest_analysis looks in ROOT/"analysis"
    d.mkdir()
    _write(d, "customer", "20260914-100000000001")
    _write(d, "customer-group", "20260914-100000000002")
    (d / "analysis-customer-20260914-100000000001.json").write_text(
        '{"doctype": "Customer"}', encoding="utf-8")
    monkeypatch.setattr(agent, "ROOT", tmp_path)

    assert agent.latest_analysis("Customer")["doctype"] == "Customer"
    assert agent.latest_analysis("Nothing Here") is None
