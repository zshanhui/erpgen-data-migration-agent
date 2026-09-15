"""Mutation check: reintroduce each P0 bug and confirm the suite catches it.

A green suite only means something if it fails when the bug is present, so this
rewrites each fix back to its buggy form, runs the targeted test, and expects a
failure. Files are always restored from an in-memory backup.

Usage:  .venv/bin/python scripts/mutation-check.py

For every mutation we assert that the targeted test FAILS with the bug present
and passes with the fix. Files are always restored from an in-memory backup.

Two ways this harness used to lie, both now reported instead:

* a mutated module that no longer exists aborted the whole run with a traceback,
  so every mutation after the first dead path silently stopped being checked;
* only pytest exit code 1 counts as CAUGHT. A node id that matches no test exits
  4, which the old `rc != 0` test scored as a catch — four parametrized node ids
  were "catching" their bug while running nothing at all.

Targeted node ids are exact, so rename or re-parametrize a test and this reports
MISSED rather than passing quietly: that is the signal to re-point the entry.
"""
from __future__ import annotations

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTX = "erpgen/context.py"
JRN = "erpgen/journal.py"
CLI = "erpgen.py"
CF = "erpgen/customers_full.py"
TLS = "erpgen/tools.py"
OVR = "erpgen/overrides.py"
ANL = "erpgen/analysis.py"
# the agent used to be one module (`erpgen/agent.py`, split in dd5dd91); the
# entries below point at the file each anchor actually lives in now
AGT = "erpgen/agent/__init__.py"      # run loop
AGTOOLS = "erpgen/agent/tools.py"     # tool wrappers + digests
AGTRACE = "erpgen/agent/trace.py"     # live stream + transcript
LLM = "erpgen/llm_providers.py"       # providers + error classification
ANA = "erpgen/analysis.py"
CFF = "erpgen/conflicts.py"
TREE = "erpgen/tree.py"
INF = "erpgen/infer.py"
EMP = "erpgen/employees.py"
DED = "erpgen/dedup.py"

MUTATIONS = [
    ("bug: update_record journals no inverse", [
        (TLS, '    if ACTIVE_JOURNAL is not None:\n        ACTIVE_JOURNAL.record_updated(doctype, name, before)',
              '    pass  # MUTANT'),
    ], "tests/test_update_record.py::test_the_effect_records_the_previous_values"),

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
    ], "tests/test_cli_args.py::test_log_dir_override_reaches_every_subcommand[map]"),

    # ---- flat party sheets (Customer/Supplier) ----
    ("bug: flat values not converted to the target field type (Check 'Yes' -> 0)", [
        (CF, '            return convert_value(raw, ftype)',
             '            return raw  # MUTANT'),
    ], "tests/test_party_sheets.py::test_check_column_is_converted_not_stored_raw[Yes-1]"),

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
        (CF, '        if status == "failed":',
             '        if False:  # MUTANT'),
    ], "tests/test_party_sheets.py::test_import_skips_contact_and_address_when_the_party_insert_fails"),

    ("bug: party-sheet detection is hardcoded to Customer", [
        (CF, '        if spec["name_column"] in hs and ("Contact Name" in hs or "Address Line 1" in hs):',
             '        if "Customer Name" in hs and ("Contact Name" in hs or "Address Line 1" in hs):  # MUTANT'),
    ], "tests/test_party_sheets.py::test_detect_customer_and_supplier_sheets"),

    ("bug: supplier is not a valid flat target", [
        (CF, '_FLAT_KEYS = frozenset(_DT_CANONICAL)',
             '_FLAT_KEYS = frozenset({"customer", "contact", "address"})  # MUTANT'),
    ], "tests/test_party_sheets.py::test_parse_flat_target_valid[supplier.tax_id-expected1]"),

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
        (CF, '        g = grouped.setdefault((header, linked), {"targets": [], "missing": []})',
             '        g = grouped.setdefault((header, f"{linked}|{kind}.{field}"), {"targets": [], "missing": []})  # MUTANT'),
    ], "tests/test_party_sheets.py::test_mirrored_and_address_targets_share_one_conflict"),

    ("bug: child-table required fields hidden from the agent", [
        (TLS, '            child = child_metas.get(f.options)',
              '            child = None  # MUTANT'),
    ], "tests/test_party_sheets.py::test_describe_doctype_exposes_child_required_fields"),

    ("bug: link check reads values from rows that never import", [
        (CF, '        values = distinct_values(source, header,\n'
             '                                 require_column=spec["name_column"])',
             '        values = distinct_values(source, header)  # MUTANT'),
    ], "tests/test_party_sheets.py::test_values_from_rows_that_cannot_import_are_ignored"),

    # ---- analysis retention ----
    ("bug: retention keeps the OLDEST analyses and deletes the newest", [
        (ANL, 'sorted(entries, key=lambda e: e[0], reverse=True)[keep:]',
              'sorted(entries, key=lambda e: e[0], reverse=False)[keep:]'),
    ], "tests/test_analysis_retention.py::test_keeps_the_newest_not_the_oldest"),

    ("bug: retention caps globally instead of per doctype", [
        (ANL, '            by_doctype.setdefault(m.group("slug"), []).append((m.group("stamp"), path))',
              '            by_doctype.setdefault("_all", []).append((m.group("stamp"), path))  # MUTANT'),
    ], "tests/test_analysis_retention.py::test_cap_is_per_doctype_not_global"),

    ("bug: retention off-by-one (drops below the cap)", [
        (ANL, 'sorted(entries, key=lambda e: e[0], reverse=True)[keep:]',
              'sorted(entries, key=lambda e: e[0], reverse=True)[keep - 1:]'),
    ], "tests/test_analysis_retention.py::test_save_analysis_prunes_automatically"),

    # ---- the decomposed run loop ----
    ("bug: every conflict treated as blocking (not just error severity)", [
        (AGT, '    return [c for c in analysis["conflicts"]\n'
              '            if c["severity"] == "error"\n'
              '            and c.get("status", "open") in ("open", "stale")]',
              '    return list(analysis["conflicts"])  # MUTANT'),
    ], "tests/test_agent_loop.py::test_error_conflicts_filters_by_severity"),

    ("bug: later rounds re-send the whole analysis instead of what failed", [
        (AGT, '    if round_no == 1:',
              '    if True:  # MUTANT'),
    ], "tests/test_agent_loop.py::test_later_rounds_get_only_what_still_fails"),

    ("bug: iteration exhaustion bails with the wrong exit code", [
        (AGT, '        return 3',
              '        return 0  # MUTANT'),
    ], "tests/test_agent_loop.py::test_iteration_exhaustion_bails_with_a_budget_hint"),

    # ---- LLM endpoint preflight ----
    ("bug: DNS failure no longer fails fast (endpoint reported reachable)", [
        (LLM, '        return False, (f"DNS lookup failed for {host}: {type(e).__name__}: {e}\\n"',
              '        return True, ""  # MUTANT\n'
              '        return False, (f"DNS lookup failed for {host}: {type(e).__name__}: {e}\\n"'),
    ], "tests/test_agent_preflight.py::test_dns_failure_is_reported_with_proxy_hints"),

    ("bug: reachable endpoint reported as unreachable (401 loses the key hint)", [
        (LLM, '        if e.code in (401, 403):',
              '        if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_reachable_401_means_the_key_is_the_problem"),

    ("bug: transport failures lose their network guidance", [
        (LLM, '    elif any(k in low for k in ("connection", "connect", "timeout", "ssl",',
              '    elif False and any(k in low for k in ("connection", "connect", "timeout", "ssl",'),
    ], "tests/test_agent_preflight.py::test_connection_error_explains_the_sdk_conflates_transport_failures"),

    ("bug: every exception dressed up as an LLM/network problem", [
        (LLM, '    if type(exc).__module__.split(".")[0] in ("openai", "httpx", "httpcore"):\n'
              '        return True',
              '    if True:  # MUTANT\n        return True'),
    ], "tests/test_agent_preflight.py::test_is_llm_error_rejects_our_own_bugs"),

    # ---- the doctype-shadowing bug (items.csv analysed as Customer) ----
    ("bug: --doctype default shadows header inference", [
        (AGT, '    ap.add_argument("--doctype", default=None,',
              '    ap.add_argument("--doctype", default="Customer",  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_doctype_flag_has_no_argparse_default"),

    ("bug: inference falls back to Customer instead of the headers", [
        (AGT, '    return args.doctype or guess_doctype(src), False',
              '    return args.doctype or "Customer", False  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_doctype_is_inferred_from_the_source_headers"),

    ("bug: flat party sheets no longer resolve to their flow", [
        (AGT, '        return flow_for_party(party), True',
              '        return None, False  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_flat_party_sheet_resolves_to_its_flow"),

    # ---- iteration budget ----
    ("bug: exhausted iteration budget not recognized", [
        (AGT, '    if "WorkflowRuntimeError" in name or "MaxIterations" in name:\n'
              '        return True',
              '    if False:  # MUTANT\n        return True'),
    ], "tests/test_agent_preflight.py::test_iteration_exhaustion_is_recognized_by_name"),

    ("bug: a reachable API root 404 blamed on --api-base", [
        (LLM, '        if e.code == 404:',
              '        if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_root_404_is_reachable_and_does_not_blame_api_base"),

    # ---- overrides file integrity ----
    ("bug: overrides written in place instead of atomically", [
        (OVR, '        with os.fdopen(fd, "w", encoding="utf-8") as fh:\n'
              '            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\\n")\n'
              '        os.replace(tmp_name, p)',
              '        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\\n",  # MUTANT\n'
              '                     encoding="utf-8")'),
    ], "tests/test_overrides.py::test_save_overrides_writes_atomically_via_rename"),

    # The agent issues set_mapping as parallel tool calls: two writers must not
    # share a temp path, and load -> modify -> save must not interleave.
    ("bug: concurrent overrides writers share one temp file", [
        (OVR, '    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".",\n'
              '                                    suffix=".tmp")',
              '    tmp_name = str(p.with_name(p.name + ".tmp"))  # MUTANT\n'
              '    fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)'),
    ], "tests/test_overrides.py::test_save_overrides_gives_each_writer_its_own_temp_file"),

    ("bug: overrides read-modify-write is not serialised", [
        (OVR, '        fcntl.flock(fh, fcntl.LOCK_EX)',
              '        pass  # MUTANT: no exclusive lock'),
    ], "tests/test_overrides.py::test_concurrent_set_mapping_keeps_both_decisions"),

    ("bug: corrupt overrides error hides the offending line", [
        (OVR, '            f"  at line {e.lineno}, column {e.colno}\\n"',
              '            ""  # MUTANT'),
    ], "tests/test_overrides.py::test_load_overrides_error_names_the_line_and_how_to_recover"),

    # ---- Dynamic Link used as a doctype name (the contacts.csv deadlock) ----
    # NOTE: this targets the "never queries it" test, not the "no conflict" test.
    # With the unverifiable-lookup fix also in place, a bogus doctype name no
    # longer *produces* a conflict (the query fails => skipped), so only the
    # "did we ask a nonsense question?" assertion still detects the regression.
    ("bug: Dynamic Link options used as a doctype name again", [
        (ANA, '        linked = t.meta.links_to_doctype',
              '        linked = t.meta.options if t.meta.is_link else None  # MUTANT'),
    ], "tests/test_dynamic_links.py::test_the_dynamic_link_is_never_queried_as_a_doctype"),

    ("bug: unqueryable linked doctype reported as all-missing", [
        (CFF, '    except UnverifiableLink:\n        return None',
              '    except UnverifiableLink:\n        return list(values)  # MUTANT'),
    ], "tests/test_dynamic_links.py::test_unqueryable_linked_doctype_is_unverifiable_not_all_missing"),

    ("bug: a failed lookup silently degrades to 'nothing exists'", [
        (CFF, "    except Exception as e:\n"
              "        if cache is not None:\n"
              "            cache[doctype] = None      # remember the failure, don't re-query\n"
              '        raise UnverifiableLink(f"{doctype}: {e}") from e',
              '    except Exception:\n'
              '        names = set()  # MUTANT'),
    ], "tests/test_dynamic_links.py::test_existing_values_raises_when_the_doctype_is_unknown"),

    # ---- agent escape hatch ----
    ("bug: stall counter never advances (agent burns every round)", [
        (AGT, '    if prev_fingerprint is not None and fingerprint and fingerprint == prev_fingerprint:\n'
              '        return stalled + 1',
              '    if False:  # MUTANT\n        return stalled + 1'),
    ], "tests/test_agent_preflight.py::test_stall_counter_starts_at_zero_then_counts_identical_rounds"),

    # ---- the agent's import must join its own run context ----
    ("bug: agent's run_import journals separately (revert misses its rows)", [
        (AGTOOLS, '    run_id = _TRANSCRIPT_CTX.get("run")\n'
              '    if run_id:',
              '    run_id = None  # MUTANT\n'
              '    if run_id:'),
    ], "tests/test_agent_preflight.py::test_run_import_propagates_the_run_id_as_a_global_flag"),

    ("bug: run id not published to the tools", [
        (AGT, '        _TRANSCRIPT_CTX.update({"round": round_no, "log_event": transcript.log,\n'
              '                                "run": getattr(args, "run", None)})',
              '        _TRANSCRIPT_CTX.update({"round": round_no, "log_event": transcript.log})  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_round_loop_publishes_the_run_id_for_tools"),

    # ---- revertibility of flat imports + link-merge ----
    ("bug: flat imports journal nothing (not revertible)", [
        (CF, '            if apply and journal:\n'
             '                journal.record_created(party, docname)',
             '            pass  # MUTANT'),
    ], "tests/test_party_sheets.py::test_flat_import_journals_every_created_record"),

    ("bug: created Contact/Address not journaled", [
        (CF, '            if journal:\n'
             '                journal.record_created(doctype, index[natural_key])',
             '            pass  # MUTANT'),
    ], "tests/test_party_sheets.py::test_flat_import_journals_every_created_record"),

    ("bug: added links are not journaled (link-merge unrevertible)", [
        (CF, '        if journal:\n'
             '            journal.link_added(doctype, name, link_doctype, link_name)',
             '        pass  # MUTANT'),
    ], "tests/test_party_sheets.py::test_flat_import_journals_the_link_merge_but_not_the_creates_it_replaces"),

    ("bug: remove_record_link drops the wrong links", [
        (JRN, '    kept = [r for r in links\n'
              '            if (r.get("link_doctype"), r.get("link_name")) != want]',
              '    kept = []  # MUTANT'),
    ], "tests/test_journal.py::test_remove_record_link_drops_only_that_link"),

    # ---- deterministic-first dispatch ----
    ("bug: clean analysis still burns LLM calls", [
        (AGT, '    if args.source and not _error_conflicts(analysis) and not args.always_llm:',
              '    if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_no_conflicts_imports_without_any_llm_call"),

    ("bug: row failures do not engage the agent", [
        (AGT, '        pre_import = warning_digest(out) or out[-1500:]\n'
              '        print(f"  {failed} row(s) failed — engaging the agent to investigate.")',
              '        return 0  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_row_failures_engage_the_agent_with_the_context"),

    ("bug: pre-import drops the run id", [
        (AGT, '        cmd = (["--run", args.run] if getattr(args, "run", None) else [])',
              '        cmd = []  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_pre_import_carries_the_run_id_before_the_subcommand"),

    ("bug: --always-llm ignored", [
        (AGT, '    if args.source and not _error_conflicts(analysis) and not args.always_llm:',
              '    if args.source and not _error_conflicts(analysis):  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_always_llm_skips_the_deterministic_pre_import"),

    ("bug: import failures misread as success", [
        (AGTOOLS, '    m = re.search(r"REST upsert: created \\d+, failed (\\d+)", out)\n'
              '    if m:\n'
              '        return int(m.group(1))',
              '    m = None  # MUTANT\n'
              '    if m:\n'
              '        return int(m.group(1))'),
    ], "tests/test_agent_preflight.py::test_import_failure_count_parses_every_output_shape[REST upsert: created 0, failed 29-29]"),

    # ---- live progress stream (so the user need not guess) ----
    ("bug: --quiet does not silence the live stream", [
        (AGTRACE, 'def _live(message: str) -> None:\n'
              '    if _LIVE["enabled"]:',
              'def _live(message: str) -> None:\n'
              '    if True:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_live_is_silent_when_disabled"),

    ("bug: LLM calls are not announced live", [
        (AGTRACE, '            _log_llm_event(event="llm_response", method="achat",\n'
              '                           duration_ms=_elapsed_ms(started),\n'
              '                           **llm_usage(response))\n'
              '            _live_llm_response(response, started)',
              '            _log_llm_event(event="llm_response", method="achat",\n'
              '                           duration_ms=_elapsed_ms(started),\n'
              '                           **llm_usage(response))  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_llm_call_is_announced_live"),

    ("bug: tool calls lose their ToolUse marker", [
        (AGTRACE, '        _live(f"{_LIVE[\'indent\']}  ToolUse:{name} {_brief_args(args, kwargs)}")',
              '        pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_tool_calls_carry_a_ToolUse_marker"),

    ("bug: a raising tool is not shown live", [
        (AGTRACE, '            _live(f"{_LIVE[\'indent\']}    ToolResult:{name} ✗ "\n'
              '                  f"{type(e).__name__}: {_brief(e, 90)}")',
              '            pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_failing_tool_calls_carry_a_ToolResult_marker"),

    ("bug: the model's thinking is hidden", [
        (AGTRACE, '        thought = _thinking(response)\n'
                   '        if thought:',
                   '        thought = ""  # MUTANT\n'
                   '        if thought:'),
    ], "tests/test_agent_preflight.py::test_live_shows_what_the_model_wants_to_do"),

    ("bug: live output loses the requested tool names", [
        (AGTRACE, '    calls = getattr(response, "tool_calls", None)\n'
              '    if not calls:\n'
              '        calls = getattr(getattr(response, "message", None), "tool_calls", None)',
              '    calls = None  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_live_shows_what_the_model_wants_to_do"),

    # ---- import failure digest (why the agent flailed on customers.csv) ----
    ("bug: failure reasons truncated away from the agent", [
        (AGTOOLS, '    return f"import exit {code}:\\n{out[-2000:]}{warning_digest(out)}"',
              '    return f"import exit {code}:\\n{out[-2000:]}"  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_run_import_result_includes_the_digest"),

    ("bug: warnings grouped by row instead of by cause", [
        (AGTOOLS, '        key = " ".join((body if sep else msg).split())[:220]',
              '        key = msg[:220]  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_warning_digest_groups_identical_causes"),

    ("bug: a shared root cause no longer flagged", [
        (AGTOOLS, '    if len(ranked) == 1 and total > 1:',
              '    if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_warning_digest_flags_a_single_shared_cause"),

    # ---- remote LLM call logging ----
    ("bug: LLM requests are not logged", [
        (AGTRACE, '            _log_llm_event(event="llm_request", method="achat",\n'
              '                           model=str(getattr(self, "model", "") or ""),\n'
              '                           messages=len(messages))',
              '            pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_every_llm_call_is_logged"),

    ("bug: failed LLM calls are not logged", [
        (AGTRACE, '                _log_llm_event(event="llm_failure", method="achat",\n'
              '                               duration_ms=_elapsed_ms(started),\n'
              '                               error=f"{type(e).__name__}: {e}")',
              '                pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_llm_failures_are_logged_and_still_raised"),

    ("bug: streaming failure at call time is not logged", [
        (AGTRACE, '                # fails before a generator exists — still a remote call attempt\n'
              '                _log_llm_event(event="llm_failure", method="astream_chat",\n'
              '                               duration_ms=_elapsed_ms(started),\n'
              '                               error=f"{type(e).__name__}: {e}")',
              '                pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_streaming_failure_is_logged"),

    ("bug: prompt content leaks into the transcript", [
        (AGTRACE, '            _log_llm_event(event="llm_request", method="achat",\n'
              '                           model=str(getattr(self, "model", "") or ""),\n'
              '                           messages=len(messages))',
              '            _log_llm_event(event="llm_request", method="achat",\n'
              '                           model=str(getattr(self, "model", "") or ""),\n'
              '                           messages=str(messages))  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_prompt_content_is_not_logged"),

    ("bug: token usage no longer recorded", [
        (AGTRACE, '            _log_llm_event(event="llm_response", method="achat",\n'
              '                           duration_ms=_elapsed_ms(started),\n'
              '                           **llm_usage(response))',
              '            _log_llm_event(event="llm_response", method="achat",\n'
              '                           duration_ms=_elapsed_ms(started))  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_llm_response_records_duration_and_tokens"),

    ("bug: escape hatch never fires (loop grinds to --max-rounds)", [
        (AGT, '        if args.max_stall_rounds and stalled >= args.max_stall_rounds:',
              '        if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_escape_hatch_fires_when_conflicts_never_change"),

    ("bug: stall counter never resets (false stall after real progress)", [
        (AGT, '    if prev_fingerprint is not None and fingerprint and fingerprint == prev_fingerprint:\n'
              '        return stalled + 1\n'
              '    return 0',
              '    if prev_fingerprint is not None and fingerprint and fingerprint == prev_fingerprint:\n'
              '        return stalled + 1\n'
              '    return stalled  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_stall_counter_resets_when_conflicts_are_resolved"),

    ("bug: `erpgen agent` no longer dispatches to the in-package agent", [
        (CLI, '    from erpgen.agent import run  # noqa: PLC0415 — keeps CLI startup cheap\n'
              '\n'
              '    return run(args)',
              '    return 0  # MUTANT'),
    ], "tests/test_cli_args.py::test_agent_subcommand_dispatches_to_the_in_package_agent"),

    ("bug: `erpgen agent` registered without its own flags", [
        (CLI, '    add_agent_flags(p_ag)', '    pass  # MUTANT'),
    ], "tests/test_cli_args.py::test_agent_doctor_runs_through_the_cli_without_an_llm"),

    # ---- self-referencing tree sheets (Customer Group & friends) ----
    # A tree sheet names its own parents: reporting those as missing blocks
    # --apply, importing child-first fails the row, and an unflagged parent
    # silently becomes a leaf with children.
    ("bug: a tree sheet's own parents reported as missing again", [
        (TREE, '    provided = sheet_identity_values(source, plan)\n'
               '    return [v for v in missing if v not in provided]',
               '    return missing  # MUTANT'),
    ], "tests/test_tree_sheets.py::test_a_self_referencing_sheets_own_parents_are_not_missing"),

    ("bug: tree rows no longer ordered parents-first", [
        (TREE, '        ready = [i for i in pending\n'
               '                 if _parent_of(payloads[i], parent_field) not in (names - done)]',
               '        ready = list(pending)  # MUTANT'),
    ], "tests/test_tree_sheets.py::test_rows_are_ordered_parents_first"),

    ("bug: intermediate tree nodes no longer marked is_group", [
        (TREE, '    if has_is_group:\n'
               '        flagged = derive_is_group(payloads, plan.id_field, parent_field)',
               '    if False:  # MUTANT\n'
               '        flagged = derive_is_group(payloads, plan.id_field, parent_field)'),
    ], "tests/test_tree_sheets.py::test_a_tree_sheet_is_ordered_and_flagged_end_to_end"),

    ("bug: group sheets no longer inferred from their headers", [
        (INF, '    ("Customer Group", ["Customer Group Name"]),',
              '    ("Customer Group", ["Not A Header"]),  # MUTANT'),
    ], "tests/test_tree_sheets.py::test_group_sheets_are_inferred_from_headers"),

    # ---- a Group node where ERPNext requires a leaf (Customer.customer_group) ----
    # ERPNext throws (HTTP 417), so every affected row fails mid-import. Reported
    # as its own kind: the record exists, so "create the missing record" is wrong.
    ("bug: a Group node accepted where the field needs a leaf", [
        (CFF, '    return [v for v in values if v in nodes]',
              '    return []  # MUTANT'),
    ], "tests/test_link_leaf.py::test_a_group_node_is_reported_as_its_own_kind"),

    ("bug: the leaf-only rule matches nothing", [
        (CFF, 'LEAF_ONLY_LINKS = {("Customer", "customer_group")}',
              'LEAF_ONLY_LINKS = set()  # MUTANT'),
    ], "tests/test_link_leaf.py::test_a_group_node_is_reported_as_its_own_kind"),

    # ...and the opposite: ERPNext only validates Customer.customer_group, so
    # widening the list invents a conflict the site does not have.
    ("bug: the leaf-only rule widened to a field ERPNext does not validate", [
        (CFF, 'LEAF_ONLY_LINKS = {("Customer", "customer_group")}',
              'LEAF_ONLY_LINKS = {("Customer", "customer_group"),\n'
              '                  ("Customer", "territory")}  # MUTANT'),
    ], "tests/test_link_leaf.py::test_a_group_territory_is_not_reported"),

    ("bug: the group-node lookup shares the name lookup's cache entry", [
        (CFF, '    key = f"!!is_group::{linked_doctype}"',
              '    key = linked_doctype  # MUTANT'),
    ], "tests/test_link_leaf.py::test_the_two_lookups_can_share_one_cache_without_colliding"),

    ("bug: flat party sheets skip the leaf check", [
        (CF, '        if (_DT_CANONICAL.get(kind, ""), field) in LEAF_ONLY_LINKS:',
             '        if False:  # MUTANT'),
    ], "tests/test_party_sheets.py::test_a_customer_group_node_is_reported_not_a_missing_record"),
    # ---- phase 1: cleaning-stage detectors -------------------------------
    ("bug: duplicate groups not aggregated per key column", [
        (CFF, '            conflicts.append(duplicate_row_conflict(key_column, groups, total, key_field))',
              '            for _g in groups:\n'
              '                conflicts.append(duplicate_row_conflict(key_column, [_g], 1, key_field))'),
    ], "tests/test_dataclean.py::test_multiple_groups_aggregate_into_one_conflict"),

    ("bug: key grouping is case-sensitive", [
        (CFF, '        gkey = raw.casefold()', '        gkey = raw  # MUTANT'),
    ], "tests/test_dataclean.py::test_case_variant_identical_is_a_warning_with_the_flag"),

    ("bug: required-field check ignores plan.defaults", [
        (CFF, '        if f.fieldname in plan.defaults or f.is_fetch_field:',
              '        if f.is_fetch_field:  # MUTANT'),
    ], "tests/test_dataclean.py::test_required_columns_helper_skips_fields_with_no_column_or_a_default"),

    ("bug: analysis lookup matches a longer doctype slug", [
        (ANL, '        if m and m.group("slug") == slug:', '        if m and slug in m.group("slug"):  # MUTANT'),
    ], "tests/test_analysis_retention.py::test_paths_match_the_doctype_slug_exactly"),

    # ---- phase 3: flat party-sheet analyser wiring ------------------------
    ("bug: flat party sheets skip the cleaning detectors", [
        (CF, '    conflicts.extend(_flat_data_quality(source, party, spec, fmap, flat, engines,\n'
             '                                        key_column))',
             '    pass  # MUTANT'),
    ], "tests/test_party_data_quality.py::test_both_analysers_report_the_same_defect_identically"),

    ("bug: flat required-cell check ignores the non-party doctypes", [
        (CF, '    for kind, mapped in by_doctype.items():',
             '    for kind, mapped in list(by_doctype.items())[:1]:  # MUTANT'),
    ], "tests/test_party_data_quality.py::test_blank_required_cells_are_checked_per_target_doctype"),

    ("bug: flat import skips the conflict gate", [
        (CLI, '    if args.apply and errs and not args.bypass_conflicts:',
              '    if False:  # MUTANT'),
    ], "tests/test_party_data_quality.py::test_flat_import_refuses_while_error_conflicts_remain"),

    # ---- phase 4: near duplicates ----------------------------------------
    ("bug: near-duplicate distance threshold loosened", [
        (CFF, 'NEAR_DUP_K = 2', 'NEAR_DUP_K = 5  # MUTANT'),
    ], "tests/test_near_duplicates.py::test_distance_three_is_rejected"),

    ("bug: identifier filter ignores uniqueness", [
        (CFF, '        if profile.unique < IDENTIFIER_UNIQUE or profile.non_empty < IDENTIFIER_POPULATED:',
              '        if False:  # MUTANT'),
    ], "tests/test_near_duplicates.py::test_low_uniqueness_columns_are_not_identifiers"),

    ("bug: exact-duplicate group order from a set of string keys", [
        (CFF, '    for gkey in order:', '    for gkey in set(groups):  # MUTANT'),
    ], "tests/test_dataclean.py::test_group_order_is_stable_across_processes"),

    ("bug: near-duplicate pairs double-report exact duplicates", [
        (CFF, '                if rows[i][5] == rows[j][5]:',
              '                if False:  # MUTANT'),
    ], "tests/test_near_duplicates.py::test_pairs_with_equal_keys_are_left_to_duplicate_row"),

    # ---- lazy journal + revert log selection ------------------------------
    # the real behaviour change was in the constructor, not in the lazy guard:
    # `_ensure_open` is only reachable from `effect()`, so mutating it is
    # equivalent when there are no effects
    ("bug: journal file created eagerly, even with no effects", [
        (JRN, '        self._fh = None\n',
              '        self._fh = self.path.open("w", encoding="utf-8")  # MUTANT\n'),
    ], "tests/test_journal.py::test_a_journal_with_no_effects_leaves_no_file"),

    ("bug: revert ignores logs with nothing to undo", [
        (CTX, '    if require_effects:', '    if False:  # MUTANT'),
    ], "tests/test_run_selection.py::test_latest_run_prefers_the_newest_log_that_has_effects"),

    ("bug: journal retention deletes an un-reverted journal", [
        (JRN, '        if effects and not reverted:\n            kept.append(f)                       # never destroy the undo path\n            continue',
              '        if False:  # MUTANT\n            kept.append(f)\n            continue'),
    ], "tests/test_journal.py::test_prune_never_deletes_a_journal_with_un_reverted_effects"),

    ("bug: journal retention order falls back to mtime", [
        (JRN, '        return (m.group("stamp") if m else "", f.stat().st_mtime)',
              '        return ("", f.stat().st_mtime)  # MUTANT'),
    ], "tests/test_journal.py::test_prune_orders_by_the_stamp_not_mtime"),

    ("bug: journal retention prunes run contexts too", [
        (JRN, '    files = sorted(d.glob(f"{JOURNAL_PREFIX}*.jsonl"), key=creation_key, reverse=True)',
              '    files = sorted(d.glob("*.jsonl"), key=creation_key, reverse=True)  # MUTANT'),
    ], "tests/test_journal.py::test_prune_ignores_run_contexts"),

    # ---- employee ID: the column that becomes the dedup key ----
    ("bug: the employee ID header alias is not recognised", [
        (EMP, '        if norm(header) in ALIASES:\n            return header',
              '        if False:  # MUTANT\n            return header'),
    ], "tests/test_employees.py::test_every_alias_spelling_is_recognised_as_the_id_column"),

    ("bug: the employee ID column keeps its scored target", [
        (EMP, '    mapping.target = target', '    pass  # MUTANT'),
    ], "tests/test_employees.py::test_the_scorer_alone_would_pick_the_wrong_field"),

    ("bug: missing employee IDs are not generated", [
        (EMP, '        p[ID_FIELD] = value', '        pass  # MUTANT'),
    ], "tests/test_employees.py::test_a_sheet_with_no_id_column_at_all_gets_ids"),

    ("bug: Employee dedup queries `name` again (duplicates every re-run)", [
        (DED, '    "Employee": {"source": "employee_number", "target": "employee_number"},',
              '    # MUTANT: no Employee key'),
    ], "tests/test_employees.py::test_the_dedup_spec_queries_employee_number_not_name"),

    # ---- the full name, and the columns with no built-in home ----
    ("bug: the full name maps to the recomputed employee_name again", [
        (EMP, '        if norm(header) not in NAME_ALIASES or not engine.parent.get("first_name"):',
              '        if True:  # MUTANT'),
    ], "tests/test_employees.py::test_full_name_maps_to_first_name_not_employee_name"),

    ("bug: Bank Branch loses its own-field rule (lands on the branch Link)", [
        (EMP, '    return OWN_FIELDS.get(norm(header))', '    return None  # MUTANT'),
    ], "tests/test_employees.py::test_bank_branch_is_not_left_on_the_office_branch_link"),

    # ---- department: docnames are `<name> - <company abbr>` ----
    ("bug: Department targets ignore the company abbreviation", [
        (EMP, '            mapping[value] = f"{value} - {abbr}"',
              '            mapping[value] = value  # MUTANT'),
    ], "tests/test_employees.py::test_department_targets_use_the_company_abbreviation"),

    ("bug: an existing Department is duplicated instead of reused", [
        (EMP, '        docname = existing.get(value)', '        docname = None  # MUTANT'),
    ], "tests/test_employees.py::test_an_existing_department_is_reused_by_name_not_duplicated"),

    ("bug: a multi-company sheet gets one per-row value_map", [
        (EMP, '    return values[0] if len(values) == 1 else ""',
              '    return values[0] if values else ""  # MUTANT'),
    ], "tests/test_employees.py::test_two_companies_stop_the_department_mapping"),

    ("bug: prerequisites stop creating the missing Department", [
        (EMP, '    lines.extend(create_missing_departments(source, client, journal))',
              '    pass  # MUTANT'),
    ], "tests/test_employees.py::test_prerequisites_create_the_field_and_the_missing_department"),

    ("bug: the analysis checks raw values instead of mapped ones", [
        (ANL, '    return [str(vmap.get(v, v)) for v in values]',
              '    return values  # MUTANT'),
    ], "tests/test_employees.py::test_link_checks_validate_the_mapped_value"),
]


def run_test(node: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", node, "-x", "--no-header", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    ).returncode


#: pytest exit codes. Only `1` means the targeted test ran and failed: anything
#: else (a node id that no longer exists, a collection error, an interrupt) used
#: to be read as "the suite caught it", which is how four parametrized node ids
#: kept claiming to catch their bug long after they matched nothing.
PYTEST_EXIT = {
    0: "the targeted test PASSED with the bug present",
    1: "failed as expected",
    2: "interrupted, or the test module failed to collect",
    3: "pytest internal error",
    4: "usage error — the node id matches no test",
    5: "no tests collected",
}


def _restore_file(rel: str, content: str) -> None:
    """Rewrite a mutated source and drop the bytecode compiled from the mutant.

    A mutation is often the same byte length as the original and is restored
    within the same second, so the `.pyc` CPython left behind still looks valid
    for the restored source and the NEXT import — the next mutation's targeted
    test, and anything run afterwards — silently executes the mutant. Observed
    with `    if require_effects:` -> `    if False:  # MUTANT` (both 23 bytes),
    which left `latest_run` preferring logs with nothing to undo long after the
    source was clean.
    """
    path = ROOT / rel
    path.write_text(content)
    try:
        Path(importlib.util.cache_from_source(str(path))).unlink()
    except OSError:
        pass


def _install_restore_guard(backups: dict) -> None:
    """Restore mutated sources on SIGTERM/SIGINT.

    `finally` does not run when the process is killed, and a killed run leaves
    the source tree MUTATED (a real bug silently reintroduced). Observed in
    practice when the suite grew past an outer command timeout.
    """
    def _restore(signum, _frame):
        for rel, content in backups.items():
            _restore_file(rel, content)
        print(f"\n  restored {len(backups)} mutated file(s) on signal {signum}",
              file=sys.stderr)
        raise SystemExit(130)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _restore)


def main() -> int:
    backups = {}
    failures = []
    _install_restore_guard(backups)
    try:
        for label, edits, node in MUTATIONS:
            for rel, old, new in edits:
                p = ROOT / rel
                if not p.exists():
                    # a moved/renamed module must not stop the whole run: report it
                    # and keep checking the rest, or one refactor hides every
                    # mutation after it (which is exactly what the agent split did)
                    print(f"  STALE {label}: {rel} does not exist")
                    failures.append((label, f"{rel} does not exist"))
                    break
                if rel not in backups:
                    backups[rel] = p.read_text()
                s = p.read_text()
                if old not in s:
                    print(f"  SKIP  {label}: anchor not found in {rel}")
                    failures.append((label, "anchor missing"))
                    break
                mutant = s.replace(old, new, 1)
                try:
                    # a mutant that cannot be imported proves nothing: the test
                    # never runs, and every non-zero exit used to read as CAUGHT.
                    # Two entries appended `# MUTANT` to a `for ... in <expr>:` line
                    # and were silently scoring as catches off a SyntaxError.
                    compile(mutant, rel, "exec")
                except SyntaxError as e:
                    print(f"  BROKEN {label}: mutation is not valid Python "
                          f"({rel}:{e.lineno}: {e.msg})")
                    failures.append((label, f"mutation does not compile: {e.msg}"))
                    break
                p.write_text(mutant)
            else:
                rc = run_test(node)
                status = "CAUGHT" if rc == 1 else "MISSED"
                print(f"  {status:<6} {label}")
                if rc != 1:
                    failures.append((label, f"exit {rc}: {PYTEST_EXIT.get(rc, '?')}"
                                            f" [{node}]"))
                # restore before the next mutation
                for rel, content in backups.items():
                    _restore_file(rel, content)
                backups.clear()
                continue
            for rel, content in backups.items():
                _restore_file(rel, content)
            backups.clear()
    finally:
        for rel, content in backups.items():
            _restore_file(rel, content)

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
