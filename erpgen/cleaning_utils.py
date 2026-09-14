"""Pure string helpers for the data cleaning stage.

Deterministic and dependency-free: no network, no LLM, no fuzzy heuristics
beyond plain Levenshtein. Nothing here reads the source sheet or the target
site — the detectors in `conflicts.py` compose these.

Used by the near-duplicate detector (see
`docs/gen/data_cleaning_plan.md`, "Possible duplicates"):

  * `compare_key` normalises a value before it is compared, so that
    `"Acme Steel Pte Ltd"` and `"acme, steel"` land on the same key.
  * `within_distance` is the confirmation step for a candidate pair.
"""
from __future__ import annotations

#: Whole tokens dropped from a comparison key. Company-form suffixes are noise
#: for identity: "Acme Steel" and "Acme Steel Pte Ltd" are the same entity.
#: Singapore/Malaysia forms included, since that is a target market.
SUFFIXES = frozenset({
    "pte", "ltd", "llc", "inc", "co", "corp", "limited", "plc",
    "sdn", "bhd", "berhad",
})


def levenshtein(a: str, b: str) -> int:
    """Plain Levenshtein edit distance: insert, delete and substitute, each cost 1.

    A transposition costs 2, not 1 — this is Levenshtein, not Damerau-Levenshtein.
    For the near-duplicate detector that is fine: `k = 2` still catches
    `"Acme"` vs `"Acem"`.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,                        # deletion
                current[j - 1] + 1,                     # insertion
                previous[j - 1] + (ca != cb),           # substitution / match
            ))
        previous = current
    return previous[-1]


def within_distance(a: str, b: str, limit: int) -> bool:
    """True when `levenshtein(a, b) <= limit`, without always computing it.

    Two cheap exits before the table is built: an exact match, and a length
    difference that already exceeds the limit. During the scan, a row whose
    minimum exceeds the limit cannot recover on later rows, so the scan stops
    there.
    """
    if a == b:
        return True
    if limit <= 0:
        return False
    if abs(len(a) - len(b)) > limit:
        return False

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        best = i
        for j, cb in enumerate(b, 1):
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            )
            current.append(value)
            if value < best:
                best = value
        if best > limit:
            return False
        previous = current
    return previous[-1] <= limit


def compare_key(value: str) -> str:
    """Normalise a value for comparison.

    Casefolded, punctuation and whitespace collapsed to single spaces, and
    company-form suffix tokens dropped. Token order is preserved — the detector
    sorts tokens separately when it wants to catch reordering.
    """
    spaced = "".join(ch if ch.isalnum() else " " for ch in (value or "").casefold())
    tokens = spaced.split()
    stripped = [t for t in tokens if t not in SUFFIXES]
    # a value made of nothing but suffix tokens ("Ltd") keeps them, so that two
    # such rows still compare as equal rather than collapsing to ""
    return " ".join(stripped or tokens)
