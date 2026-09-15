"""RestLoader surfaces per-row import failures on stderr.

The agent only sees the import's stdout tail, so without this it knows
"failed: N" but not why, and burns rounds guessing (the suppliers_dirty loop).
"""
from __future__ import annotations

from erpgen.client import ERPNextError
from erpgen.loader import RestLoader


class _Boom:
    """A client whose `insert` always fails, like a validation rejection."""

    def insert(self, doctype: str, doc: dict):
        raise ERPNextError('Supplier Type cannot be "Hardware"')


def test_rest_loader_prints_a_warning_per_failed_row(capsys):
    RestLoader(_Boom()).upsert(
        "Supplier",
        [{"__row": 11, "supplier_name": "Ironclad Fasteners"}],
        key_field="supplier_name",
    )
    err = capsys.readouterr().err
    assert "WARNING: row 11: Supplier 'Ironclad Fasteners' failed:" in err
    assert 'Supplier Type cannot be "Hardware"' in err


def test_rest_loader_reports_only_failures(capsys):
    class _Ok:
        def insert(self, doctype, doc):
            return {"name": doc["supplier_name"]}

    RestLoader(_Ok()).upsert(
        "Supplier",
        [{"__row": 2, "supplier_name": "Apex Industrial Supply"}],
        key_field="supplier_name",
    )
    assert capsys.readouterr().err == ""
