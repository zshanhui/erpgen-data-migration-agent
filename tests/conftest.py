"""Shared fixtures and factories for the P0 unit tests.

These tests are **pure**: no network, no live ERPNext site, no Docker. Everything
is built in-process or written to pytest's `tmp_path`.

Run:  .venv/bin/python -m pytest
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erpgen.client import ERPNextError  # noqa: E402
from erpgen.context import MigrationContext  # noqa: E402
from erpgen.metadata import DoctypeMeta, FieldMeta  # noqa: E402
from erpgen.source import SourceTable  # noqa: E402


# ------------------------------------------------- cross-process determinism
def run_isolated(script: str, seeds: tuple = ("0", "1", "12345")) -> set[str]:
    """Run `script` in fresh interpreters with different hash seeds.

    Python randomises str/bytes hashing per process, so anything that builds
    reported order by iterating a set of *strings* varies between runs. Repeating
    a call inside one process cannot catch that — the seed is fixed for the whole
    process — so the check has to be cross-process.
    """
    import os
    import subprocess
    import sys

    outputs = set()
    for seed in seeds:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip())
    return outputs


# --------------------------------------------------------------------- CLI
@pytest.fixture(scope="session")
def cli():
    """The `erpgen.py` script, loaded by path.

    `import erpgen` resolves to the *package* of the same name, so the CLI
    module must be loaded explicitly under its own module name.
    """
    if "erpgen_cli" not in sys.modules:
        spec = importlib.util.spec_from_file_location("erpgen_cli", ROOT / "erpgen.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["erpgen_cli"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["erpgen_cli"]


# --------------------------------------------------------------- agent module
@pytest.fixture(scope="session")
def agent_mod():
    """`erpgen.agent` — the agent now lives inside the package."""
    import erpgen.agent as agent_mod
    return agent_mod


# ----------------------------------------------------------------- factories
def make_field(fieldname: str, label: str | None = None, fieldtype: str = "Data",
               **kw) -> FieldMeta:
    return FieldMeta(fieldname=fieldname, label=label or fieldname,
                     fieldtype=fieldtype, **kw)


def make_meta(name: str, fields: list, autoname: str | None = None,
              istable: bool = False, is_submittable: bool = False) -> DoctypeMeta:
    """Build a DoctypeMeta the same way `DoctypeMeta.fetch` does (via from_api,
    which is what populates the fieldname index)."""
    raw_fields = [
        f.as_dict() if isinstance(f, FieldMeta) else dict(f) for f in fields
    ]
    return DoctypeMeta.from_api({
        "name": name,
        "autoname": autoname,
        "istable": istable,
        "is_submittable": is_submittable,
        "fields": raw_fields,
    })


def make_sheet(headers: list[str], rows: list[list] | None = None) -> SourceTable:
    """A SourceTable with profiles, built without touching the filesystem.

    Profiles come from the real `build_profiles`, so `unique`, `non_empty` and
    `inferred_type` reflect the data. Hand-rolling them once meant every column
    looked 100% unique, which made column-selection logic (e.g. deciding which
    columns are identifiers) untestable and misleading.
    """
    rows = rows if rows is not None else [[f"{h}-1"] for h in headers]
    return SourceTable(name="test", headers=list(headers), rows=rows).build_profiles()


@pytest.fixture
def mkctx(tmp_path: Path):
    """Factory for a MigrationContext rooted in tmp_path (one per run id)."""
    def _mk(run_id: str = "run1", **kw) -> MigrationContext:
        return MigrationContext(run_id, tmp_path, **kw)
    return _mk


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


# -------------------------------------------------------------- fake client
class FakeClient:
    """Duck-typed ERPNextClient for the pure revert/apply paths.

    Only the methods those paths actually call are implemented; every call is
    recorded so tests can assert order and idempotency.
    """

    def __init__(self, existing: tuple = (), fail_with: dict | None = None,
                 records: dict | None = None):
        #: {(doctype, name)} that still exist on the "site"
        self.existing = set(existing)
        self.calls: list[tuple] = []
        self.fail_with = fail_with or {}
        #: {doctype: [row, ...]} returned by list(); empty means "no records yet"
        self.records = records or {}

    def list(self, doctype, filters=None, fields=None, limit=0, **kw):
        self.calls.append(("list", doctype))
        return list(self.records.get(doctype, []))

    def delete(self, doctype: str, name: str) -> None:
        self.calls.append(("delete", doctype, name))
        if doctype in self.fail_with:
            raise ERPNextError(self.fail_with[doctype])
        if (doctype, name) not in self.existing:
            # what the real client surfaces for a missing doc
            raise ERPNextError(
                'HTTP 404 DELETE /api/resource/'
                f'{doctype}/{name}: {{"exc_type":"DoesNotExistError"}}'
            )
        self.existing.discard((doctype, name))


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


# ------------------------------------------------------------ journal files
def write_journal(path: Path, effects: list[dict], run_id: str = "j1",
                  doctype: str = "Item", extra_lines: list[str] | None = None,
                  raw_prefix: str = "") -> Path:
    """Write a journal/run JSONL file with the given effect entries."""
    lines = [json.dumps({"event": "run_start", "run_id": run_id, "doctype": doctype})]
    for i, e in enumerate(effects, 1):
        lines.append(json.dumps({"event": "effect", "seq": i, **e}))
    lines.extend(extra_lines or [])
    path.write_text(raw_prefix + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def delete_record_effect(doctype: str, name: str) -> dict:
    return {"kind": "record_create",
            "inverse": {"op": "delete_record", "doctype": doctype, "name": name},
            "doctype": doctype, "name": name}
