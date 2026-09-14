"""Tests for the pure cleaning helpers: edit distance and comparison keys.

Table-driven over known string pairs, plus a property check that
`within_distance` agrees with `levenshtein` across a range of limits — the
bounded version has an early exit, and that exit is exactly where it could
disagree.
"""
from __future__ import annotations

import time

import pytest

from erpgen.cleaning_utils import SUFFIXES, compare_key, levenshtein, within_distance

#: (a, b, expected distance) — the classics, plus the pairs this project cares
#: about: a 1-character typo in a party name, and CJK, since the sample sheets
#: carry Chinese names.
DISTANCES = [
    ("", "", 0),
    ("abc", "abc", 0),
    ("", "abc", 3),
    ("abc", "", 3),
    ("a", "", 1),
    ("kitten", "sitting", 3),
    ("flaw", "lawn", 2),
    ("saturday", "sunday", 3),
    ("Acme", "Akme", 1),            # substitution
    ("Acme", "Acmee", 1),           # insertion
    ("Acme", "Acm", 1),             # deletion
    ("Acme", "Acem", 2),            # transposition costs 2 (not Damerau)
    ("Acme Steel Works", "Acme Steel Work", 1),
    ("Acme Steel", "Acme Steel Pte Ltd", 8),   # raw distance is large: the
                                               # reason compare_key exists
    ("Tan Wei Ming", "Tan Wei Min", 1),
    ("陈伟明", "陈伟朋", 1),          # CJK substitution
    ("陈伟明", "林美玲", 3),
    ("", "陈", 1),
]


@pytest.mark.parametrize("a,b,expected", DISTANCES)
def test_levenshtein_known_distances(a, b, expected):
    assert levenshtein(a, b) == expected


@pytest.mark.parametrize("a,b,_expected", DISTANCES)
def test_levenshtein_is_symmetric(a, b, _expected):
    assert levenshtein(a, b) == levenshtein(b, a)


def test_levenshtein_bounds():
    """Distance never exceeds the longer string's length."""
    for a, b, _ in DISTANCES:
        assert levenshtein(a, b) <= max(len(a), len(b))


def test_levenshtein_does_not_mutate_inputs():
    a, b = "Acme Steel", "Acme Steal"
    levenshtein(a, b)
    assert (a, b) == ("Acme Steel", "Acme Steal")


# ------------------------------------------------------------ within_distance
@pytest.mark.parametrize("a,b,expected", [
    ("Acme", "Acme", 0),
    ("Acme", "Akme", 1),
    ("Acme", "Acem", 2),
    ("Acme", "Acme Steel", 6),
    ("Acme Steel Works", "Acme Steel Work", 1),
    ("陈伟明", "陈伟朋", 1),
])
def test_within_distance_at_the_exact_limit(a, b, expected):
    assert within_distance(a, b, expected) is True


@pytest.mark.parametrize("a,b,expected", [
    ("Acme", "Akme", 1),
    ("Acme", "Acem", 2),
    ("Acme", "Acme Steel", 6),
    ("kitten", "sitting", 3),
    ("陈伟明", "林美玲", 3),
])
def test_within_distance_one_below_the_limit_is_false(a, b, expected):
    if expected == 0:
        pytest.skip("limit -1 is not meaningful")
    assert within_distance(a, b, expected - 1) is False


def test_within_distance_zero_means_equality():
    assert within_distance("Acme", "Acme", 0) is True
    assert within_distance("Acme", "acme", 0) is False
    assert within_distance("Acme", "Akme", 0) is False


def test_within_distance_rejects_on_length_difference():
    """The length pre-reject must not turn a near match into a miss."""
    assert within_distance("Acme", "Acme Steel", 2) is False
    assert within_distance("Acme", "Acmex", 2) is True   # length diff 1, distance 1


@pytest.mark.parametrize("a,b,_expected", DISTANCES)
@pytest.mark.parametrize("limit", [0, 1, 2, 3, 4, 8])
def test_within_distance_agrees_with_levenshtein(a, b, _expected, limit):
    """The early exit must never change the answer."""
    assert within_distance(a, b, limit) == (levenshtein(a, b) <= limit)


def test_within_distance_is_symmetric():
    for a, b, _ in DISTANCES:
        for limit in (1, 2, 3):
            assert within_distance(a, b, limit) == within_distance(b, a, limit)


def test_within_distance_short_circuits_on_a_long_mismatch():
    """The row-minimum cutoff is a performance path, so pin it with a time bound.

    These strings have equal length (the length pre-reject cannot fire) and no
    matching characters, so the row minimum reaches 3 at row 3 and the scan stops
    there. The full table would be 25M cell updates, which in CPython is tens of
    seconds — hence a bound far outside normal jitter.
    """
    a, b = "a" * 5000, "b" * 5000
    start = time.perf_counter()
    assert within_distance(a, b, 2) is False
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"took {elapsed:.2f}s — the early exit did not fire"


def test_within_distance_short_circuits_on_a_length_mismatch():
    """The length pre-reject is the only defence when the two strings share a long
    prefix: the row minimum stays 0 all the way through it, so the row-min cutoff
    never fires and the table would be ~64M cell updates.
    """
    a = "acme" * 2000                 # 8000 characters
    b = a + "xxx"                     # same prefix; length difference 3 > k
    start = time.perf_counter()
    assert within_distance(a, b, 2) is False
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"took {elapsed:.2f}s — the length pre-reject did not fire"


# --------------------------------------------------------------- compare_key
@pytest.mark.parametrize("value,expected", [
    ("Acme Steel", "acme steel"),
    ("ACME STEEL", "acme steel"),
    ("  Acme   Steel  ", "acme steel"),
    ("Acme, Steel & Co.", "acme steel"),          # "Co" is a suffix token
    ("Acme Steel Pte Ltd", "acme steel"),
    ("Acme Steel Pte. Ltd.", "acme steel"),
    ("Acme Steel Sdn Bhd", "acme steel"),
    ("Acme (Suzhou) Co., Ltd.", "acme suzhou"),
    ("Acme-Steel", "acme steel"),
    ("", ""),
    ("   ", ""),
    ("Ltd", "ltd"),                               # all-suffix guard
    ("Pte Ltd", "pte ltd"),
    ("陈伟明", "陈伟明"),                           # CJK preserved
    ("陈伟明 (Tan Wei Ming)", "陈伟明 tan wei ming"),
    ("Steel Acme", "steel acme"),                 # order preserved, not sorted
    ("1001", "1001"),
])
def test_compare_key(value, expected):
    assert compare_key(value) == expected


def test_compare_key_collapses_known_variants():
    """The variants the near-duplicate detector must treat as the same key."""
    variants = ["Acme Steel", "acme  steel", "ACME STEEL PTE LTD",
                "Acme, Steel & Co.", "Acme Steel Sdn. Bhd."]
    keys = {compare_key(v) for v in variants}
    assert keys == {"acme steel"}, keys


def test_compare_key_is_idempotent():
    for value in ("Acme Steel Pte Ltd", "陈伟明 (Tan Wei Ming)", "  x  "):
        assert compare_key(compare_key(value)) == compare_key(value)


def test_compare_key_then_distance_finds_spelling_variants():
    """End to end: normalise, then confirm within the detector's k = 2."""
    a = compare_key("Acme Steel Works Pte Ltd")
    b = compare_key("Acme Steel Work")
    assert (a, b) == ("acme steel works", "acme steel work")
    assert within_distance(a, b, 2) is True


def test_suffixes_are_whole_tokens_only():
    """A name containing a suffix-looking substring must not be mangled."""
    assert compare_key("Costa Coffee") == "costa coffee"   # "co" inside words
    assert compare_key("Coca Cola") == "coca cola"
    assert "co" in SUFFIXES
