"""Mutation check: reintroduce each P0 bug and confirm the suite catches it.

A green suite only means something if it fails when the bug is present, so this
rewrites each fix back to its buggy form, runs the targeted test, and expects a
failure. Files are always restored from an in-memory backup.

Usage:  .venv/bin/python scripts/mutation-check.py

For every mutation we assert that the targeted test FAILS with the bug present
and passes with the fix. Files are always restored from an in-memory backup.
"""
from __future__ import annotations

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
AGT = "erpgen/agent.py"
ANA = "erpgen/analysis.py"
CFF = "erpgen/conflicts.py"

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
              'sorted(entries, key=lambda e: e[0], reverse=False)[keep:]  # MUTANT'),
    ], "tests/test_analysis_retention.py::test_keeps_the_newest_not_the_oldest"),

    ("bug: retention caps globally instead of per doctype", [
        (ANL, '            by_doctype.setdefault(m.group("slug"), []).append((m.group("stamp"), path))',
              '            by_doctype.setdefault("_all", []).append((m.group("stamp"), path))  # MUTANT'),
    ], "tests/test_analysis_retention.py::test_cap_is_per_doctype_not_global"),

    ("bug: retention off-by-one (drops below the cap)", [
        (ANL, 'sorted(entries, key=lambda e: e[0], reverse=True)[keep:]',
              'sorted(entries, key=lambda e: e[0], reverse=True)[keep - 1:]  # MUTANT'),
    ], "tests/test_analysis_retention.py::test_save_analysis_prunes_automatically"),

    # ---- the decomposed run loop ----
    ("bug: every conflict treated as blocking (not just error severity)", [
        (AGT, '    return [c for c in analysis["conflicts"] if c["severity"] == "error"]',
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
        (AGT, '        return False, (f"DNS lookup failed for {host}: {type(e).__name__}: {e}\\n"',
              '        return True, ""  # MUTANT\n'
              '        return False, (f"DNS lookup failed for {host}: {type(e).__name__}: {e}\\n"'),
    ], "tests/test_agent_preflight.py::test_dns_failure_is_reported_with_proxy_hints"),

    ("bug: reachable endpoint reported as unreachable (401 loses the key hint)", [
        (AGT, '        if e.code in (401, 403):',
              '        if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_reachable_401_means_the_key_is_the_problem"),

    ("bug: transport failures lose their network guidance", [
        (AGT, '    elif any(k in low for k in ("connection", "connect", "timeout", "ssl",',
              '    elif False and any(k in low for k in ("connection", "connect", "timeout", "ssl",'),
    ], "tests/test_agent_preflight.py::test_connection_error_explains_the_sdk_conflates_transport_failures"),

    ("bug: every exception dressed up as an LLM/network problem", [
        (AGT, '    if type(exc).__module__.split(".")[0] in ("openai", "httpx", "httpcore"):\n'
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
        (AGT, '        if e.code == 404:',
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
        (AGT, '    run_id = _TRANSCRIPT_CTX.get("run")\n'
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
        (AGT, '    m = re.search(r"REST upsert: created \\d+, failed (\\d+)", out)\n'
              '    if m:\n'
              '        return int(m.group(1))',
              '    m = None  # MUTANT\n'
              '    if m:\n'
              '        return int(m.group(1))'),
    ], "tests/test_agent_preflight.py::test_import_failure_count_parses_every_output_shape"),

    # ---- live progress stream (so the user need not guess) ----
    ("bug: --quiet does not silence the live stream", [
        (AGT, 'def _live(message: str) -> None:\n'
              '    if _LIVE["enabled"]:',
              'def _live(message: str) -> None:\n'
              '    if True:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_live_is_silent_when_disabled"),

    ("bug: LLM calls are not announced live", [
        (AGT, '            _log_llm_event(event="llm_response", method="achat",\n'
              '                           duration_ms=_elapsed_ms(started),\n'
              '                           **llm_usage(response))\n'
              '            _live_llm_response(response, started)',
              '            _log_llm_event(event="llm_response", method="achat",\n'
              '                           duration_ms=_elapsed_ms(started),\n'
              '                           **llm_usage(response))  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_llm_call_is_announced_live"),

    ("bug: tool calls lose their ToolUse marker", [
        (AGT, '        _live(f"{_LIVE[\'indent\']}  ToolUse:{name} {_brief_args(args, kwargs)}")',
              '        pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_tool_calls_carry_a_ToolUse_marker"),

    ("bug: a raising tool is not shown live", [
        (AGT, '            _live(f"{_LIVE[\'indent\']}    ToolResult:{name} ✗ "\n'
              '                  f"{type(e).__name__}: {_brief(e, 90)}")',
              '            pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_failing_tool_calls_carry_a_ToolResult_marker"),

    ("bug: the model's thinking is hidden", [
        (AGT, '    thought = _thinking(response)\n'
              '    if thought:\n'
              '        _live(f"{_LIVE[\'indent\']}    “{thought}”")',
              '    pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_live_shows_what_the_model_wants_to_do"),

    ("bug: live output loses the requested tool names", [
        (AGT, '    calls = getattr(response, "tool_calls", None)\n'
              '    if not calls:\n'
              '        calls = getattr(getattr(response, "message", None), "tool_calls", None)',
              '    calls = None  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_live_shows_what_the_model_wants_to_do"),

    # ---- import failure digest (why the agent flailed on customers.csv) ----
    ("bug: failure reasons truncated away from the agent", [
        (AGT, '    return f"import exit {code}:\\n{out[-2000:]}{warning_digest(out)}"',
              '    return f"import exit {code}:\\n{out[-2000:]}"  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_run_import_result_includes_the_digest"),

    ("bug: warnings grouped by row instead of by cause", [
        (AGT, '        key = " ".join((body if sep else msg).split())[:220]',
              '        key = msg[:220]  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_warning_digest_groups_identical_causes"),

    ("bug: a shared root cause no longer flagged", [
        (AGT, '    if len(ranked) == 1 and total > 1:',
              '    if False:  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_warning_digest_flags_a_single_shared_cause"),

    # ---- remote LLM call logging ----
    ("bug: LLM requests are not logged", [
        (AGT, '            _log_llm_event(event="llm_request", method="achat",\n'
              '                           model=str(getattr(self, "model", "") or ""),\n'
              '                           messages=len(messages))',
              '            pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_every_llm_call_is_logged"),

    ("bug: failed LLM calls are not logged", [
        (AGT, '                _log_llm_event(event="llm_failure", method="achat",\n'
              '                               duration_ms=_elapsed_ms(started),\n'
              '                               error=f"{type(e).__name__}: {e}")',
              '                pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_llm_failures_are_logged_and_still_raised"),

    ("bug: streaming failure at call time is not logged", [
        (AGT, '                # fails before a generator exists — still a remote call attempt\n'
              '                _log_llm_event(event="llm_failure", method="astream_chat",\n'
              '                               duration_ms=_elapsed_ms(started),\n'
              '                               error=f"{type(e).__name__}: {e}")',
              '                pass  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_streaming_failure_is_logged"),

    ("bug: prompt content leaks into the transcript", [
        (AGT, '            _log_llm_event(event="llm_request", method="achat",\n'
              '                           model=str(getattr(self, "model", "") or ""),\n'
              '                           messages=len(messages))',
              '            _log_llm_event(event="llm_request", method="achat",\n'
              '                           model=str(getattr(self, "model", "") or ""),\n'
              '                           messages=str(messages))  # MUTANT'),
    ], "tests/test_agent_preflight.py::test_prompt_content_is_not_logged"),

    ("bug: token usage no longer recorded", [
        (AGT, '            _log_llm_event(event="llm_response", method="achat",\n'
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
]


def run_test(node: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", node, "-x", "--no-header", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    ).returncode


def _install_restore_guard(backups: dict) -> None:
    """Restore mutated sources on SIGTERM/SIGINT.

    `finally` does not run when the process is killed, and a killed run leaves
    the source tree MUTATED (a real bug silently reintroduced). Observed in
    practice when the suite grew past an outer command timeout.
    """
    def _restore(signum, _frame):
        for rel, content in backups.items():
            (ROOT / rel).write_text(content)
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
