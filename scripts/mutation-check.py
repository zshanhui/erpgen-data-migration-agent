"""Mutation check: reintroduce each P0 bug and confirm the suite catches it.

A green suite only means something if it fails when the bug is present, so this
rewrites each fix back to its buggy form, runs the targeted test, and expects a
failure. Files are always restored from an in-memory backup.

Usage:  .venv/bin/python scripts/mutation-check.py

For every mutation we assert that the targeted test FAILS with the bug present
and passes with the fix. Files are always restored from an in-memory backup.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTX = "erpgen/context.py"
JRN = "erpgen/journal.py"
CLI = "erpgen.py"
CF = "erpgen/customers_full.py"
TLS = "erpgen/tools.py"

MUTATIONS = [
    ("bug: effect seq restarts per command", [
        (CTX, '                    self.effects += 1\n                    effects.append(e)',
              '                    pass  # MUTANT\n                    effects.append(e)'),
    ], "tests/test_context_effects.py::test_effect_sequence_continues_across_commands"),

    ("bug: pending requirements lost on reopen", [
        (CTX, '                    self._pending[e.get("id")] = e',
              '                    pass  # MUTANT'),
    ], "tests/test_context_requirements.py::test_requirements_are_visible_to_a_later_command"),

    ("bug: partial coverage closes a multi-value requirement", [
        (CTX, '                    if set(missing) <= done:',
              '                    if True:  # MUTANT'),
    ], "tests/test_context_requirements.py::test_link_requirement_requires_every_missing_value"),

    ("bug: duplicate requirements on re-analysis", [
        (CTX, '            if ident in self._ident_pending:\n                continue',
              '            if False:  # MUTANT\n                continue'),
    ], "tests/test_context_requirements.py::test_identical_requirement_is_not_recorded_twice"),

    ("bug: undone fix does not reopen the requirement", [
        (CTX, '            reopened = ident in self._ident_done',
              '            reopened = False  # MUTANT'),
    ], "tests/test_context_requirements.py::test_satisfied_requirement_reopens_when_it_reappears"),

    ("bug: effect ownership of pre-existing data", [
        (CTX, '        self._react(kind, seq=0, condition=True, **info)',
              '        self._react(kind, seq=0, **info)'),
    ], "tests/test_context_requirements.py::test_condition_satisfies_requirement_without_journaling_an_effect"),

    ("bug: double close raises", [
        (CTX, '        if self._closed:                      # closing twice must not raise\n            return\n',
              ''),
    ], "tests/test_context_effects.py::test_close_is_idempotent"),

    ("bug: latest_run sorts by filename not mtime", [
        (CTX, '    cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)',
              '    cands.sort(reverse=True)  # MUTANT'),
    ], "tests/test_run_selection.py::test_latest_run_is_newest_by_mtime_not_filename"),

    # the shared predicate: one mutation, two consumers must catch it
    ("bug: already_reverted accepts a partial marker", [
        (JRN, '    if last.get("status") == "ok" and last.get("reverted", 0) >= len(data["effects"]):',
              '    if True:  # MUTANT'),
    ], "tests/test_journal.py::test_partial_marker_does_not_count_as_reverted"),

    ("bug: latest_run does not skip reverted files", [
        (JRN, '    markers = [e for e in data.get("extra", []) if e.get("event") == "revert"]',
              '    return None  # MUTANT\n'
              '    markers = [e for e in data.get("extra", []) if e.get("event") == "revert"]'),
    ], "tests/test_run_selection.py::test_latest_run_ignores_a_reverted_file"),

    ("bug: already_reverted ignores the marker status", [
        (JRN, '    if last.get("status") == "ok" and last.get("reverted", 0) >= len(data["effects"]):',
              '    if last.get("reverted", 0) >= len(data["effects"]):  # MUTANT'),
    ], "tests/test_run_selection.py::test_latest_run_does_not_skip_a_failed_revert_marker"),

    ("bug: reverting twice replays the journal", [
        (JRN, '    if marker and apply and not force:',
              '    if False and apply and not force:  # MUTANT'),
    ], "tests/test_journal.py::test_second_revert_is_a_no_op"),

    ("bug: already-gone record reported as FAILED", [
        (JRN, '        if op in ("delete_record", "delete_custom_field") and \\\n'
              '                ("DoesNotExistError" in msg or "404" in msg):\n'
              '            return True, ""\n',
              ''),
    ], "tests/test_journal.py::test_apply_inverse_treats_missing_record_as_success"),

    ("bug: a failed revert is marked as reverted", [
        (JRN, '    if apply and not failed and results:',
              '    if apply:  # MUTANT'),
    ], "tests/test_journal.py::test_failed_revert_is_not_marked_so_it_can_be_retried"),

    ("bug: --log-dir only on the import subparser", [
        (CLI, '    ap.add_argument("--log-dir", default="logs",\n'
              '                    help="audit log directory (default: logs/)")\n',
              ''),
        (CLI, '    p_imp.add_argument("--defaults")',
              '    p_imp.add_argument("--defaults")\n'
              '    p_imp.add_argument("--log-dir", default="logs")'),
    ], "tests/test_cli_args.py::test_every_subcommand_carries_the_global_flags"),

    # ---- flat party sheets (Customer/Supplier) ----
    ("bug: flat values not converted to the target field type (Check 'Yes' -> 0)", [
        (CF, '            return convert_value(raw, ftype)',
             '            return raw  # MUTANT'),
    ], "tests/test_party_sheets.py::test_check_column_is_converted_not_stored_raw"),

    ("bug: Supplier.country no longer mirrored onto the party record", [
        (CF, '        for col, field in (spec.get("mirror_columns") or {}).items():',
             '        for col, field in ({}).items():  # MUTANT'),
    ], "tests/test_party_sheets.py::test_build_payloads_mirrors_country_onto_the_supplier"),

    ("bug: link-merge hardcodes the Customer link type again", [
        (CF, '    links.append({"link_doctype": link_doctype, "link_name": link_name})',
             '    links.append({"link_doctype": "Customer", "link_name": link_name})  # MUTANT'),
    ], "tests/test_party_sheets.py::test_link_merge_adds_a_second_party_link"),

    ("bug: a failed row is remembered, so a re-run cannot retry it", [
        (CF, "        warn(f\"row {row_no}: {doctype} '{natural_key}' failed: {e}\")\n"
             '        return "failed", str(e)',
             "        warn(f\"row {row_no}: {doctype} '{natural_key}' failed: {e}\")\n"
             "        index[natural_key] = None  # MUTANT\n"
             '        return "failed", str(e)'),
    ], "tests/test_party_sheets.py::test_link_or_create_failure_is_retryable"),

    ("bug: contact/address created for a party that failed to insert", [
        (CF, '        if pstatus == "failed":',
             '        if False:  # MUTANT'),
    ], "tests/test_party_sheets.py::test_import_skips_contact_and_address_when_the_party_insert_fails"),

    ("bug: party-sheet detection is hardcoded to Customer", [
        (CF, '        if spec["name_column"] in hs and ("Contact Name" in hs or "Address Line 1" in hs):',
             '        if "Customer Name" in hs and ("Contact Name" in hs or "Address Line 1" in hs):  # MUTANT'),
    ], "tests/test_party_sheets.py::test_detect_customer_and_supplier_sheets"),

    ("bug: supplier is not a valid flat target", [
        (CF, '_FLAT_DOCTYPES = {"customer", "supplier", "contact", "address"}',
             '_FLAT_DOCTYPES = {"customer", "contact", "address"}  # MUTANT'),
    ], "tests/test_party_sheets.py::test_parse_flat_target_valid"),

    ("bug: mapped Link values are never checked against the site", [
        (CF, '    conflicts.extend(_link_value_conflicts(client, source, spec, fmap, flat, engines))',
             '    pass  # MUTANT'),
    ], "tests/test_party_sheets.py::test_link_conflict_for_a_mapped_but_missing_link_value"),

    ("bug: link check skips the fixed contract columns", [
        (CF, '    for header, (kind, field) in fmap.items():\n'
             '        if kind == "contact" and field in CONTACT_COLUMNS.values():\n'
             '            continue  # synthetic contact keys, not real columns\n'
             '        targets.append((header, kind, field))',
             '    pass  # MUTANT'),
    ], "tests/test_party_sheets.py::test_contract_link_columns_are_validated"),

    ("bug: one column reports per target instead of per linked doctype", [
        (CF, '        g = grouped.setdefault((header, fmeta.options), {"targets": [], "missing": []})',
             '        g = grouped.setdefault((header, f"{fmeta.options}|{kind}.{field}"), {"targets": [], "missing": []})  # MUTANT'),
    ], "tests/test_party_sheets.py::test_mirrored_and_address_targets_share_one_conflict"),

    ("bug: child-table required fields hidden from the agent", [
        (TLS, '            child = child_metas.get(f.options)',
              '            child = None  # MUTANT'),
    ], "tests/test_party_sheets.py::test_describe_doctype_exposes_child_required_fields"),
]


def run_test(node: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", node, "-x", "--no-header", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    ).returncode


def main() -> int:
    backups = {}
    failures = []
    try:
        for label, edits, node in MUTATIONS:
            for rel, old, new in edits:
                p = ROOT / rel
                if rel not in backups:
                    backups[rel] = p.read_text()
                s = p.read_text()
                if old not in s:
                    print(f"  SKIP  {label}: anchor not found in {rel}")
                    failures.append((label, "anchor missing"))
                    break
                p.write_text(s.replace(old, new, 1))
            else:
                rc = run_test(node)
                status = "CAUGHT" if rc != 0 else "MISSED"
                print(f"  {status:<6} {label}")
                if rc == 0:
                    failures.append((label, "test passed with the bug present"))
                # restore before the next mutation
                for rel, content in backups.items():
                    (ROOT / rel).write_text(content)
                backups.clear()
                continue
            for rel, content in backups.items():
                (ROOT / rel).write_text(content)
            backups.clear()
    finally:
        for rel, content in backups.items():
            (ROOT / rel).write_text(content)

    print()
    if failures:
        print(f"{len(failures)} mutation(s) not caught:")
        for label, why in failures:
            print(f"   - {label}: {why}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
