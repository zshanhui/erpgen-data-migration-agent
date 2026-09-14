"""Phase 4: near-duplicate detection (review-only, warning).

Table-driven over the string pairs this project cares about, plus the guards that
keep signal B from becoming noise: a near-unique *numeric* column and a sparse
free-text column are not identifiers, and pairs with equal keys belong to
`duplicate_row`.

Pure: synthetic sheets, no client. The determinism test spawns subprocesses
because string hashing — and therefore set iteration order — is randomised per
process, so an in-process repeat cannot catch the trap it guards.
"""
from __future__ import annotations

import pytest
from conftest import ROOT, make_field, make_meta, make_sheet, run_isolated

from erpgen.analysis import build_analysis
from erpgen.conflicts import (IDENTIFIER_UNIQUE, MAX_PAIRS_ROWS, identifier_columns,
                              possible_duplicate_pairs,
                              possible_duplicate_row_conflict)
from erpgen.mapper import MappingEngine

HEADERS = ["Customer Name", "Email", "Group", "Credit Limit", "Notes"]

#: ten names with no shared tokens, so no pair of them is a near-duplicate
DISTINCT = ["Alpha Foundry", "Bravo Logistics", "Charlie Metals", "Delta Plastics",
            "Echo Robotics", "Foxtrot Trading", "Golf Supplies", "Hotel Parts",
            "India Castings", "Juliet Tools"]


def _pairs(rows, key="Customer Name", **kw):
    sheet = make_sheet(HEADERS, rows)
    pairs, count, skipped = possible_duplicate_pairs(sheet, key, **kw)
    return pairs, count, skipped


def _two(name_a, name_b, **kw):
    """Two rows differing only in the key, so only signal A can fire."""
    rows = [[name_a, "a@x.example", "Commercial", "15000", ""],
            [name_b, "b@y.example", "Commercial", "8000", ""]]
    return _pairs(rows, **kw)


# ------------------------------------------------------------- signal A
@pytest.mark.parametrize("a,b,label", [
    ("Acme Steel", "Acme Steal", "substitution"),
    ("Acme Steel", "Acme Steeel", "insertion"),
    ("Acme Steel", "Acme Stee", "deletion"),
])
def test_one_character_edits_are_detected(a, b, label):
    pairs, count, _ = _two(a, b)
    assert count == 1, label
    assert pairs[0]["a"]["value"] == a or pairs[0]["a"]["value"] == b
    assert "key_fuzzy" in pairs[0]["signals"]
    assert pairs[0]["edit_distance"] <= 2


def test_distance_three_is_rejected():
    """`k = 2` is the boundary: one more edit and it is a different name."""
    assert _two("Acme", "Acmxyz")[1] == 0
    assert _two("Acme", "Acmx")[1] == 1          # distance 2 still caught


def test_suffix_only_difference_is_detected():
    pairs, count, _ = _two("Acme Steel", "Acme Steel Pte Ltd")
    assert count == 1
    assert "key_fuzzy" in pairs[0]["signals"]
    assert pairs[0]["edit_distance"] == 0        # compare_key drops the suffix


def test_token_reorder_is_detected():
    pairs, count, _ = _two("Acme Steel Works", "Steel Works Acme")
    assert count == 1
    assert "token_reorder" in pairs[0]["signals"]
    assert pairs[0]["edit_distance"] > 2         # only the signature catches this


def test_pairs_with_equal_keys_are_left_to_duplicate_row():
    """Reporting them here would double-count one defect."""
    pairs, count, _ = _two("Acme Steel", "acme steel  ")
    assert count == 0 and pairs == []


def test_blank_keys_are_left_to_missing_value():
    rows = [["", "a@x.example", "Commercial", "15000", ""],
            ["", "b@y.example", "Commercial", "8000", ""]]
    assert _pairs(rows)[1] == 0


# ------------------------------------------------------------- signal B
def _identifier_sheet():
    """10 distinct names, with one email shared by two of them.

    10 rows is the minimum that keeps the column at `>= 0.9` unique once one value
    is shared — `(N-1)/N >= 0.9` — which is exactly the tension in the threshold.
    """
    rows = [[name, f"p{i}@x.example", "Commercial", str(1000 + i), ""]
            for i, name in enumerate(DISTINCT)]
    rows[1][1] = rows[0][1]                     # same email, unrelated names
    return rows


def test_shared_identifier_is_detected_despite_different_names():
    pairs, count, _ = _pairs(_identifier_sheet())
    assert count == 1
    (pair,) = pairs
    assert pair["signals"] == ["shared_identifier"]     # signal A cannot see this
    assert pair["shared_fields"] == ["Email"]
    assert pair["a"]["row"] == 2 and pair["b"]["row"] == 3


def test_low_uniqueness_columns_are_not_identifiers():
    """`Group` is shared legitimately, so it is not evidence of a duplicate row."""
    sheet = make_sheet(HEADERS, [
        [DISTINCT[0], "a@x.example", "Commercial", "15000", ""],
        [DISTINCT[1], "b@y.example", "Commercial", "8000", ""],
        [DISTINCT[2], "c@z.example", "Commercial", "7000", ""],
    ])
    assert "Group" not in identifier_columns(sheet, "Customer Name")
    assert possible_duplicate_pairs(sheet, "Customer Name")[1] == 0


def test_a_near_unique_numeric_column_is_not_an_identifier():
    """A credit limit is near-unique and populated, but sharing one means nothing."""
    rows = [[name, f"p{i}@x.example", "Wholesale", "5000", ""]
            for i, name in enumerate(DISTINCT)]
    sheet = make_sheet(HEADERS, rows)
    identifiers = identifier_columns(sheet, "Customer Name")
    assert "Credit Limit" not in identifiers       # numeric: sharing means nothing
    assert "Email" in identifiers                  # the control: still an identifier
    assert possible_duplicate_pairs(sheet, "Customer Name")[1] == 0


def test_a_sparse_free_text_column_is_not_an_identifier():
    """`Notes` is near-unique by nature and only a third populated."""
    rows = [[name, f"p{i}@x.example", "Wholesale", str(1000 + i),
             "same note" if i < 3 else ""] for i, name in enumerate(DISTINCT)]
    sheet = make_sheet(HEADERS, rows)
    assert "Notes" not in identifier_columns(sheet, "Customer Name")
    assert possible_duplicate_pairs(sheet, "Customer Name")[1] == 0


# ------------------------------------------------------- report properties
def test_findings_are_warnings_never_errors():
    pairs, count, skipped = _two("Acme Steel", "Acme Steal")
    conflict = possible_duplicate_row_conflict("Customer Name", pairs, count,
                                               "customer_name", skipped)
    assert conflict["severity"] == "warning"
    assert conflict["kind"] == "possible_duplicate_row"
    assert "review" in conflict["suggested_action"]


def test_pair_list_is_capped_with_the_true_count():
    rows = [[f"Acme Steel {i}", f"p{i}@x.example", "Commercial", str(i), ""]
            for i in range(12)]
    pairs, count, _ = _pairs(rows, cap=3)
    assert count == len(rows) * (len(rows) - 1) // 2      # all pairs are near
    assert len(pairs) == 3


def test_pairs_carry_shared_and_differing_fields():
    rows = _identifier_sheet()
    rows[1][3] = "9999"                                   # credit limit differs
    pairs, count, _ = _pairs(rows)
    (pair,) = pairs
    assert pair["shared_fields"] == ["Email"]
    assert pair["differing_fields"] == ["Credit Limit"]


def test_pair_order_is_stable_across_processes(tmp_path):
    """Guards the set-iteration trap: string hashing is randomised per process, so
    a report built from a set varies between runs even when the data does not."""
    csv_path = tmp_path / "names.csv"
    names = ["Acme Steel", "Acme Steal", "Acme Steel Co", "Beta Works",
             "Beta Work", "Gamma Trading", "Gamma Tradng", "Delta Supplies"]
    csv_path.write_text("Customer Name\n" + "\n".join(names) + "\n", encoding="utf-8")

    script = (
        "import json\n"
        "from erpgen.source import read_csv\n"
        "from erpgen.conflicts import possible_duplicate_pairs\n"
        f"sheet = read_csv({str(csv_path)!r})\n"
        "pairs, count, _ = possible_duplicate_pairs(sheet, 'Customer Name')\n"
        "print(json.dumps([p['a']['row'] for p in pairs]))\n"
    )
    assert len(run_isolated(script)) == 1, "pair order changed between processes"


# ------------------------------------------------------------ the size guard
def test_signal_a_is_skipped_above_the_row_guard(monkeypatch):
    rows = [[name, f"p{i}@x.example", "Wholesale", str(i), ""]
            for i, name in enumerate(DISTINCT[:6])]
    pairs, count, skipped = _pairs(rows, max_pairs_rows=3)
    assert skipped is True
    assert count == 0                     # signal B has nothing to match here
    conflict = possible_duplicate_row_conflict("Customer Name", pairs, count, "", skipped)
    assert conflict["signal_a_skipped"] is True
    assert "quadratic" in conflict["detail"]


def test_signal_b_still_runs_when_signal_a_is_skipped():
    rows = _identifier_sheet()            # 10 rows; guard set below that
    pairs, count, skipped = _pairs(rows, max_pairs_rows=5)
    assert skipped is True and count == 1
    assert pairs[0]["signals"] == ["shared_identifier"]


def test_guard_default_is_the_documented_constant():
    assert MAX_PAIRS_ROWS == 2000
    assert IDENTIFIER_UNIQUE == 0.9


# ------------------------------------------------------------------ wiring
def test_analysis_carries_the_possible_duplicate_conflict():
    engine = MappingEngine(make_meta("Customer", [
        make_field("customer_name", "Customer Name", reqd=True),
        make_field("email_id", "Email"),
    ]))
    sheet = make_sheet(["Customer Name", "Email"], [
        ["Acme Steel", "a@x.example"],
        ["Acme Steal", "b@y.example"],
        ["Beta Works", "c@z.example"],
    ])
    analysis = build_analysis(FakeStub(), sheet, engine.suggest(sheet), engine,
                              id_column="Customer Name")
    kinds = {c["kind"]: c["severity"] for c in analysis["conflicts"]}
    assert kinds["possible_duplicate_row"] == "warning"
    assert "- possible_duplicate_row ->" in analysis["agent_instructions"]


class FakeStub:
    """build_analysis only reads `list` for Link columns; this sheet has none."""

    def list(self, *a, **kw):
        return []


# ------------------------------------------------- the fixture sheets
#: (path, key column, expected pairs as (row_a, row_b, signals))
SAMPLES = [
    ("samples/customers_near_dup.csv", "Customer Name", [
        (2, 3, ("key_fuzzy",)),                        # deletion
        (4, 5, ("key_fuzzy",)),                        # transposition, 2 edits
        (6, 7, ("key_fuzzy", "token_reorder")),        # legal suffix only
        (8, 9, ("token_reorder",)),                    # reordered words
        (10, 11, ("shared_identifier",)),              # shared email, unrelated names
    ]),
    ("samples/suppliers_near_dup.csv", "Supplier Name", [
        (2, 3, ("key_fuzzy",)),
        (4, 5, ("key_fuzzy",)),                        # insertion
        (6, 7, ("token_reorder",)),
        (8, 9, ("key_fuzzy",)),
        (10, 11, ("shared_identifier",)),
    ]),
]


@pytest.mark.parametrize("path,key,expected", SAMPLES)
def test_sample_sheets_flag_exactly_the_injected_pairs(path, key, expected):
    """Pins the fixtures: rows 12/13 are a control pair that must stay unflagged."""
    from erpgen.source import read_csv

    pairs, count, _ = possible_duplicate_pairs(read_csv(ROOT / path), key)
    got = [(p["a"]["row"], p["b"]["row"], tuple(p["signals"])) for p in pairs]
    assert got == expected
    assert count == len(expected)


@pytest.mark.parametrize("path,key,_expected", SAMPLES)
def test_sample_sheets_are_clean_apart_from_the_review_pairs(path, key, _expected):
    """The only findings are warnings, so the sheets stay importable — that is what
    makes them usable for demonstrating that a near duplicate does not gate."""
    from erpgen.conflicts import data_quality_conflicts
    from erpgen.source import read_csv

    source = read_csv(ROOT / path)
    assert data_quality_conflicts(source, key_column=key) == []
