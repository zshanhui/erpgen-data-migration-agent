#!/usr/bin/env python3
"""erpgen CLI — map & migrate spreadsheets into ERPNext.

Idempotent imports: before inserting anything, the tool queries which natural
keys already exist on the target site. Only NEW records are created; duplicates
are skipped; every row (created / skipped / failed) is written to an audit log.

Examples:
  # inspect a source and get a mapping plan (metadata comes from the live site)
  python3 erpgen.py map samples/customers.csv --doctype Customer

  # dry-run: plan + payloads + predicted skips, touches nothing
  python3 erpgen.py import samples/customers.csv --doctype Customer \
      --defaults '{"customer_group":"Commercial","territory":"All Territories"}'

  # import: creates new records, skips existing, logs everything
  # (--defaults is optional; pass real JSON, not a placeholder)
  python3 erpgen.py import samples/customers.csv --doctype Customer --apply
  python3 erpgen.py import samples/customers.csv --doctype Customer \
      --defaults '{"customer_group":"Commercial","territory":"All Territories"}' --apply

  # bulk path via the Data Import machinery (also deduped), optional submit
  python3 erpgen.py import samples/customers.csv --doctype Customer --apply --bulk --submit

  # explicit key column (needed when it isn't auto-inferred)
  python3 erpgen.py import samples/sales_orders.csv --doctype "Sales Order" \
      --id-column "Sales Order ID" --apply

  # create a new column on a doctype (e.g. for an unmapped source column)
  python3 erpgen.py createfield Customer --label "Vendor Code" --fieldtype Data
  python3 erpgen.py createfield Customer --list

  # inspect records (agent verification / existence checks)
  python3 erpgen.py get-record Customer "Acme Steel Works"
  python3 erpgen.py list-records "Customer Group"
  python3 erpgen.py list-records "Customer Group" --filter '[["name","like","%Whol%"]]'
  python3 erpgen.py describe-doctype "Customer Group"   # what a record needs

  # revert a migration (replays the run journal's inverses: records, custom
  # fields and mapping overrides in one command)
  python3 erpgen.py revert --latest Item
  python3 erpgen.py revert --latest Item --apply

  # clean up demo data
  python3 erpgen.py delete --doctype Customer --names "Acme Steel Works,Bluedot Logistics"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from erpgen.agent import add_agent_flags  # noqa: E402
from erpgen.analysis import build_analysis, save_analysis  # noqa: E402
from erpgen.client import ERPNextClient  # noqa: E402
from erpgen.conflicts import (  # noqa: E402
    data_quality_conflicts,
    possible_duplicate_pairs,
    possible_duplicate_row_conflict,
    required_mapped_columns,
)
from erpgen.context import (  # noqa: E402
    MigrationContext,
    latest_run,
    load_run,
    resolve_run,
)
from erpgen.dedup import (  # noqa: E402
    DEDUP_KEYS,
    dedup_key_label,
    dedup_payloads,
    existing_keys,
    existing_names,
    extract_key,
    infer_id_column,
)
from erpgen.infer import guess_doctype  # noqa: E402
from erpgen.journal import (  # noqa: E402
    MigrationJournal,
    parse_journal,
    revert_journal,
)
from erpgen.loader import DataImportLoader, RestLoader  # noqa: E402
from erpgen.logger import RunLogger  # noqa: E402
from erpgen.mapper import MappingEngine  # noqa: E402
from erpgen.metadata import DoctypeMeta, fetch_with_children  # noqa: E402
from erpgen.customers_full import (  # noqa: E402
    build_party_sheet_analysis,
    detect_party_sheet,
    flat_map_for,
    flow_for_party,
    load_flat_mappings,
    parse_flat_target,
    party_for_flow,
    run_flat_parties_import,
)
from erpgen.overrides import (  # noqa: E402
    DEFAULT_OVERRIDES,
    apply_overrides,
    load_overrides,
    set_mapping,
    unset_mapping,
)
from erpgen.source import read_source  # noqa: E402
from erpgen.tools import (  # noqa: E402
    create_field,
    create_record,
    describe_doctype,
    get_record,
    list_records,
)
from erpgen.tree import apply_tree_semantics  # noqa: E402

DEFAULT_BASE = "http://localhost:8082"


def _context(args, doctype: str = "", source: str = "", command: str = ""):
    """Shared migration context when --run <id> is given, else None.

    With a run id, every command appends requirements/effects to ONE file, so
    `revert <run-id>` undoes the whole migration.
    """
    run = getattr(args, "run", None)
    if not run:
        return None
    return MigrationContext(
        run,
        log_dir=getattr(args, "log_dir", "logs"),
        source=source,
        base_url=getattr(args, "base", ""),
        doctypes=[doctype] if doctype else None,
        command=command,
    )


def _effect_sink(args, doctype: str = "", source: str = "", command: str = ""):
    """Where effects are journaled: the run context, or a per-command journal."""
    ctx = _context(args, doctype, source, command)
    if ctx is not None:
        return ctx
    return MigrationJournal(
        getattr(args, "log_dir", "logs"),
        doctype=doctype or "migration",
        source=source or command,
        base_url=getattr(args, "base", ""),
    )


def _check_id_column(args, source) -> bool:
    """An explicit --id-column must exist in the sheet.

    Returns False (after printing) so callers can exit 2. Without this, an
    unknown column fell through to inference and the user was told "cannot
    determine the ID column ... pass --id-column", which misleads precisely
    because they did pass one.
    """
    explicit = getattr(args, "id_column", None)
    if explicit and source.column_index(explicit) is None:
        print(f"ERROR: --id-column {explicit!r} is not a column in "
              f"{args.source}. Available columns: {', '.join(source.headers)}",
              file=sys.stderr)
        return False
    return True


def _client(args) -> ERPNextClient:
    return ERPNextClient(
        args.base, username=args.user, password=args.password, timeout=args.timeout
    )


def _engine(args, doctype: str) -> tuple[MappingEngine, ERPNextClient]:
    client = _client(args)
    parent, children = fetch_with_children(client, doctype)
    defaults = {}
    if args.defaults:
        try:
            defaults = json.loads(args.defaults)
        except json.JSONDecodeError as e:
            print(
                f"ERROR: --defaults must be valid JSON, got: {args.defaults!r}\n"
                f"  ({e})\n"
                f"  Example: --defaults '{{\"customer_group\":\"Commercial\"}}'\n"
                f"  (omit --defaults if not needed)",
                file=sys.stderr,
            )
            raise SystemExit(2)
    engine = MappingEngine(parent, children, doctype_defaults=defaults)
    return engine, client


def _print_plan(plan, source) -> None:
    print(f"\nMapping plan for {plan.doctype}  ({source.n_rows} source rows)")
    print(f"{'SOURCE COLUMN':<28}{'TARGET FIELD':<32}{'SCORE':<7}{'METHOD':<12}NOTES")
    for m in plan.mappings:
        tgt = m.target or "(unmapped)"
        notes = "; ".join(m.notes)
        print(f"{m.source:<28}{tgt:<32}{m.confidence:<7.2f}{m.method:<12}{notes}")
    if plan.defaults:
        print(f"\nDefaults applied: {json.dumps(plan.defaults)}")
    if plan.warnings:
        print("\nWarnings:")
        for w in plan.warnings:
            print(f"  ! {w}")
    if plan.fetch_from_conflicts:
        print("\nFetch-from conflicts (must be written via source doc):")
        for c in plan.fetch_from_conflicts:
            print(f"  !! {c}")
    if plan.link_fields:
        print("\nLink fields (values validated against the target site):")
        for l in plan.link_fields:
            print(f"  -> {l}")


def _inject_id_column(source, payloads, plan, id_column: str) -> None:
    """Put the source ID value into the payload under the id_field when the
    source column isn't already mapped there (e.g. transactions with --id-column)."""
    id_field = plan.id_field or "name"
    col_idx = source.column_index(id_column)
    if col_idx is None:
        return
    for p in payloads:
        ridx = p.get("__row")
        if ridx is None or ridx - 2 >= len(source.rows):
            continue
        row = source.rows[ridx - 2]
        if col_idx < len(row) and str(row[col_idx]).strip():
            p[id_field] = str(row[col_idx]).strip()


# ------------------------------------------------------------------ commands
def _overrides_for(args, doctype: str) -> tuple[Optional[str], dict]:
    """Resolve the overrides file: explicit --overrides, else the default file
    if it exists. Returns (path_or_None, per-doctype overrides dict)."""
    path = args.overrides
    if path is None and Path(DEFAULT_OVERRIDES).exists():
        path = DEFAULT_OVERRIDES
    if path is None:
        return None, {}
    data = load_overrides(path)
    return path, data.get(doctype, {})


def cmd_map(args) -> int:
    source = read_source(args.source)
    # an unknown explicit --id-column is a hard error, before any mapping work
    if not _check_id_column(args, source):
        return 2
    party = detect_party_sheet(source)
    if party:
        # flat party sheet: fixed contract + out-of-contract columns for the LLM
        client = _client(args)
        flow = flow_for_party(party)
        flat_mappings = load_flat_mappings(args.overrides or DEFAULT_OVERRIDES, flow)
        analysis = build_party_sheet_analysis(
            client, source, party, base_url=args.base, source_path=args.source,
            flat_mappings=flat_mappings,
        )
        apath = save_analysis(analysis, args.analysis_dir)
        print(f"{flow} analysis saved to {apath}")
        print(f"  {len(analysis['known_mappings'])} contract columns, "
              f"{len(analysis['extra_columns'])} out-of-contract column(s)")
        for c in analysis["conflicts"]:
            # the kind is part of the line: two conflicts on one column (a blank
            # key and a duplicate key) are otherwise indistinguishable
            print(f"  [{c['severity']:<7}] {c['kind']:<18} {c['source']:<18} "
                  f"-> {c.get('target') or c.get('suggested_action')}")
        return 0
    if not args.doctype:
        args.doctype = guess_doctype(source)
        if not args.doctype:
            print("ERROR: could not infer doctype from headers; pass --doctype.",
                  file=sys.stderr)
            return 2
        print(f"Inferred doctype: {args.doctype}")
    print(f"Source: {source.name} | {len(source.headers)} columns x {source.n_rows} rows")
    for p in source.profiles:
        extra = f" [{', '.join(p.messy)}]" if p.messy else ""
        print(f"  - {p.header:<28} type={p.inferred_type:<6} nonempty={p.non_empty:.0%} "
              f"unique={p.unique:.0%}{extra}  sample={p.sample[:3]}")
    engine, client = _engine(args, args.doctype)
    plan = engine.suggest(source)
    o_path, overrides = _overrides_for(args, plan.doctype)
    if overrides:
        n = apply_overrides(plan, source, engine, overrides)
        if n:
            print(f"Applied {n} mapping override(s) from {o_path}")
    _print_plan(plan, source)
    if args.save:
        Path(args.save).write_text(json.dumps(plan.as_dict(), indent=2))
        print(f"\nPlan saved to {args.save}")

    # agent-consumable analysis artifact (plan + profiles + conflicts + actions)
    analysis = build_analysis(
        client, source, plan, engine,
        id_column=infer_id_column(plan, source, args.id_column),
        base_url=args.base, source_path=args.source,
    )
    apath = save_analysis(analysis, args.analysis_dir)
    print(f"Analysis saved to {apath}  "
          f"({len(analysis['conflicts'])} conflicts, "
          f"{len(analysis['suggested_custom_fields'])} suggested custom fields)")

    ctx = _context(args, plan.doctype, args.source, command="map")
    if ctx:
        n = ctx.add_requirements(analysis["conflicts"])
        ctx.close()
        print(f"Run context: {ctx.path}  ({n} requirement(s) recorded)")
    return 0


def _import_flat_party_sheet(args, source, party: str) -> int:
    """Flat party sheet: split one file into party + Contact + Address."""
    client = _client(args)
    flow = flow_for_party(party)
    defaults = json.loads(args.defaults) if args.defaults else {}
    flat_mappings = load_flat_mappings(args.overrides or DEFAULT_OVERRIDES, flow)

    # ---- analysis + fail-before-apply gate ----
    # The relational path refuses while error-severity conflicts remain; a flat
    # row feeds THREE doctypes at once, so an unresolved error here can corrupt
    # all three. Same contract, same escape hatch.
    analysis = build_party_sheet_analysis(
        client, source, party, base_url=args.base, source_path=args.source,
        flat_mappings=flat_mappings)
    apath = save_analysis(analysis, args.analysis_dir)
    print(f"Analysis saved to {apath}  ({len(analysis['conflicts'])} conflicts)")

    errs = [c for c in analysis["conflicts"] if c["severity"] == "error"]
    if args.apply and errs and not args.bypass_conflicts:
        print(f"\nERROR: {len(errs)} error-severity conflict(s) remain:")
        for c in errs:
            print(f"  [{c['kind']}] {c.get('source') or c.get('field')}")
        print("Resolve them (set-mapping / createfield / create_record) and re-run, "
              "or pass --bypass-conflicts to import anyway.")
        return 2

    logger = RunLogger(args.log_dir, tag=flow) if args.apply else None
    journal = (_effect_sink(args, doctype=party, source=args.source, command="import")
               if args.apply else None)
    if logger:
        logger.run_start(source=args.source, base=args.base, apply=args.apply)
    run_flat_parties_import(
        client, source, party=party, defaults=defaults, apply=args.apply,
        logger=logger, flat_mappings=flat_mappings, journal=journal,
    )
    if logger:
        logger.run_end()
    if journal:
        journal.close(status="ok")
        n = journal.count if hasattr(journal, "count") else getattr(journal, "effects", 0)
        print(journal.summary())
    _report_out_of_contract(args, client, source, party, flat_mappings)
    return 0


def _report_out_of_contract(args, client, source, party: str, flat_mappings: dict) -> None:
    """Surface columns outside the flat contract so an agent can resolve them."""
    contract = flat_map_for(party)
    extra = [h for h in source.headers if h not in contract and h not in flat_mappings]
    if not extra:
        return
    analysis = build_party_sheet_analysis(
        client, source, party, base_url=args.base, source_path=args.source,
        flat_mappings=flat_mappings,
    )
    apath = save_analysis(analysis, args.analysis_dir)
    print(f"\nNOTE: {len(extra)} column(s) outside the flat contract are "
          f"dropped at import.")
    print(f"Analysis saved to {apath}  "
          f"({len(analysis['conflicts'])} conflicts, "
          f"{len(analysis['suggested_custom_fields'])} suggested custom fields)")


def _open_import_run(args, plan, analysis: dict, row_errors: list,
                     key_label: str, id_column: str):
    """Open the audit log and the journal / unified run context.

    Only called when applying, so callers can rely on a live logger/journal.
    """
    mode = "bulk (Data Import)" if args.bulk else "upsert (REST)"
    logger = RunLogger(args.log_dir, tag=f"import-{plan.doctype.lower().replace(' ', '-')}")
    ctx = _context(args, plan.doctype, args.source, command="import")
    if ctx:
        ctx.add_requirements(analysis["conflicts"])
    journal = ctx or MigrationJournal(args.log_dir, doctype=plan.doctype,
                                      source=args.source, base_url=args.base)
    logger.run_start(
        doctype=plan.doctype,
        source=args.source,
        base=args.base,
        mode=mode,
        id_field=plan.id_field,
        key_field=key_label,
        id_column=id_column,
        submit=args.submit,
        defaults=plan.defaults,
        plan=plan.as_dict(),
    )
    for e in row_errors:
        logger.row(e["row"], "", "failed", message="; ".join(e["errors"]))
    return logger, journal, ctx


def _predicted_dedup(client, plan, payloads: list, spec, key_label: str):
    """(source keys, keys that already exist) — read-only, so dry-run safe."""
    if spec:
        keys = [k for k in (extract_key(p, spec["source"]) for p in payloads) if k]
        existing = existing_keys(client, plan.doctype, spec["target"]) if keys else set()
    else:
        keys = [str(p.get(key_label) or "").strip() for p in payloads if p.get(key_label)]
        existing = (existing_names(client, plan.doctype, plan.id_field or "name", keys)
                    if keys else set())
    return keys, existing


def _print_dry_run_preview(args, engine, plan, payloads: list) -> None:
    print("\nDry run (use --apply to import). ")
    if args.bulk:
        csv_text = engine.build_template_csv(plan, payloads)
        print("CSV preview (bulk path):")
        print("\n".join(csv_text.splitlines()[:6]))
    else:
        print("First payload (REST upsert path):")
        print(json.dumps({k: v for k, v in payloads[0].items() if k != "__row"},
                         indent=2) if payloads else "{}")


def _load_payloads(args, client, engine, plan, to_create: list, skipped: list,
                   existing: set, key_label: str, logger, journal) -> None:
    """Push the new rows via the Data Import machinery or REST upsert."""
    if args.bulk:
        if not to_create:
            print("Nothing new to import; skipping Data Import run.")
            return
        csv_text = engine.build_template_csv(plan, to_create)
        result = DataImportLoader(client).load(
            plan.doctype,
            csv_text,
            import_type="Insert New Records",
            submit_after_import=args.submit,
            skipped=len(skipped),
            timeout=args.timeout,
            logger=logger,
            journal=journal,
        )
        print(json.dumps(result.as_dict(), indent=2))
        return
    results = RestLoader(client).upsert(
        plan.doctype,
        to_create,
        key_label,
        existing=existing,
        submit=args.submit,
        logger=logger,
        journal=journal,
    )
    ok = sum(1 for r in results if r.ok and not r.skipped)
    print(f"REST upsert: created {ok}, failed {sum(1 for r in results if not r.ok)}")


def _verified_created(client, plan, spec, to_create: list, key_label: str) -> int:
    """Re-read the site and count how many of the new keys really landed."""
    if spec:
        created = [k for k in (extract_key(p, spec["source"]) for p in to_create) if k]
        return sum(1 for k in created if k in existing_keys(client, plan.doctype,
                                                            spec["target"]))
    created = [str(p.get(key_label) or "").strip() for p in to_create if p.get(key_label)]
    return len(existing_names(client, plan.doctype, plan.id_field or "name",
                              [k for k in created if k]))


def _build_plan(args, source):
    """Score the mapping, apply overrides, and build the payloads.

    Returns `(engine, client, plan, payloads, row_errors)`.
    """
    engine, client = _engine(args, args.doctype)
    plan = engine.suggest(source)
    overrides_path, overrides = _overrides_for(args, plan.doctype)
    if overrides and apply_overrides(plan, source, engine, overrides):
        print(f"Applied mapping override(s) from {overrides_path}")
    _print_plan(plan, source)

    payloads, row_errors = engine.build_payloads(source, plan)
    payloads, tree_warnings = apply_tree_semantics(engine, plan, payloads)
    for w in tree_warnings:
        print(f"NOTE: {w}")
    if row_errors:
        print(f"\n{len(row_errors)} rows failed value conversion:")
        for e in row_errors[:10]:
            print(f"  row {e['row']}: {e['errors']}")
    if plan.fetch_from_conflicts:
        print("NOTE: fetch_from fields were dropped from payloads (see conflicts above)")
    return engine, client, plan, payloads, row_errors


def cmd_import(args) -> int:
    source = read_source(args.source)

    # Flat party sheet (inline contact/address columns) -> split into
    # party + Contact + Address internally, same as any other import.
    party = detect_party_sheet(source)
    if party:
        return _import_flat_party_sheet(args, source, party)

    if not args.doctype:
        args.doctype = guess_doctype(source)
        if not args.doctype:
            print("ERROR: could not infer doctype from headers; pass --doctype.",
                  file=sys.stderr)
            return 2
        print(f"Inferred doctype: {args.doctype}")

    # an unknown explicit --id-column is a hard error, before any mapping work
    if not _check_id_column(args, source):
        return 2

    engine, client, plan, payloads, row_errors = _build_plan(args, source)

    # ---- idempotency setup ----
    id_column = infer_id_column(plan, source, args.id_column)
    if id_column is None:
        print(f"\nERROR: cannot determine the ID column for {plan.doctype} "
              f"(id field: {plan.id_field!r}). Pass --id-column <source column>.")
        return 2
    if args.id_column:
        _inject_id_column(source, payloads, plan, args.id_column)

    key_label = dedup_key_label(plan.doctype, plan, payloads)
    spec = DEDUP_KEYS.get(plan.doctype)
    print(f"\nPrepared {len(payloads)} payloads for {plan.doctype}")
    print(f"  id_field: {plan.id_field!r} | key: {key_label!r} | id_column: {id_column!r}")

    # ---- agent-consumable analysis artifact (saved on dry-run AND apply) ----
    analysis = build_analysis(
        client, source, plan, engine,
        id_column=id_column, base_url=args.base, source_path=args.source,
    )
    apath = save_analysis(analysis, args.analysis_dir)
    print(f"Analysis saved to {apath}  "
          f"({len(analysis['conflicts'])} conflicts, "
          f"{len(analysis['suggested_custom_fields'])} suggested custom fields)")

    # ---- run log (opened before any write so failures are logged) ----
    logger = journal = ctx = None
    if args.apply:
        logger, journal, ctx = _open_import_run(args, plan, analysis, row_errors,
                                                key_label, id_column)

    # ---- predicted dedup (read-only, safe in dry-run too) ----
    keys, existing = _predicted_dedup(client, plan, payloads, spec, key_label)
    predicted_new = [k for k in keys if k not in existing]
    print(f"  of {len(keys)} keyed rows: {len(predicted_new)} new, "
          f"{len(keys) - len(predicted_new)} already exist (will be skipped)")

    if not args.apply:
        _print_dry_run_preview(args, engine, plan, payloads)
        return 0

    errs = [c for c in analysis["conflicts"] if c["severity"] == "error"]
    if errs and not args.bypass_conflicts:
        print(f"\nERROR: {len(errs)} error-severity conflict(s) remain:")
        for c in errs:
            print(f"  [{c['kind']}] {c.get('source') or c.get('field')}")
        print("Resolve them (set-mapping / createfield / create_record) and re-run, "
              "or pass --bypass-conflicts to import anyway.")
        return 2

    to_create, skipped = dedup_payloads(
        client, plan.doctype, plan, payloads, id_column=id_column, logger=logger
    )
    print(f"\nDedup: {len(to_create)} to create, {len(skipped)} skipped")
    _load_payloads(args, client, engine, plan, to_create, skipped, existing,
                   key_label, logger, journal)

    # ---- post-run verification ----
    logger.run_end(verified_created=_verified_created(client, plan, spec, to_create,
                                                      key_label),
                   new_keys=len(to_create))
    journal.close()
    print()
    print(logger.summary(plan.doctype, args.source))
    if ctx:
        print(f"Run context: {journal.path}  ({journal.effects} effect(s), "
              f"{len(journal.pending_requirements())} requirement(s) still pending)")
        print(f"  revert the whole migration: python3 erpgen.py revert {args.run} --apply")
    else:
        print(journal.summary())
    return 0


def _snake(label: str) -> str:
    """Derive a snake_case fieldname from a label, like ERPNext does."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip()).strip("_").lower()
    return re.sub(r"_+", "_", s)


def cmd_createfield(args) -> int:
    client = _client(args)
    doctype = args.doctype

    if args.list:
        cfs = client.list(
            "Custom Field",
            filters=[["dt", "=", doctype]],
            fields=["name", "fieldname", "label", "fieldtype", "reqd", "read_only"],
            limit=0,
        )
        if not cfs:
            print(f"No custom fields on {doctype}")
            return 0
        print(f"Custom fields on {doctype}:")
        for cf in cfs:
            flags = []
            if cf.get("reqd"):
                flags.append("reqd")
            if cf.get("read_only"):
                flags.append("read-only")
            print(f"  {cf['name']:<44} {cf.get('fieldtype',''):<9} "
                  f"{cf.get('label','')}  ({', '.join(flags)})")
        return 0

    if not args.label:
        print("ERROR: --label is required (or use --list)", file=sys.stderr)
        return 2

    result = create_field(
        client,
        doctype,
        args.label,
        fieldtype=args.fieldtype,
        fieldname=args.fieldname,
        options=args.options,
        reqd=args.reqd,
        read_only=args.read_only,
        insert_after=args.insert_after,
        fetch_from=args.fetch_from,
        default=args.default,
    )
    if result["created"]:
        print(f"Created {result['name']} ({args.fieldtype}) on {doctype}")
        sink = _effect_sink(args, doctype, source="createfield", command="createfield")
        sink.custom_field_created(doctype, result["fieldname"], result["name"],
                                  label=args.label)
        sink.close()
        label = "Run context" if getattr(args, "run", None) else "Journal"
        print(f"{label}: {sink.path}")
    else:
        print(f"Field already exists: {result['name']} (nothing to do)")
        return 0

    # verify the mapper can now see it (custom fields are merged into metadata)
    meta = DoctypeMeta.fetch(client, doctype)
    f = meta.get(result["fieldname"])
    if f:
        print(f"Verified: mapper now discovers field '{f.fieldname}' (label '{f.label}')")
    else:
        print("WARNING: field created but not yet visible in metadata (cache?)")
    return 0


def _json_arg(name: str, value) -> Optional[list]:
    """Parse a JSON CLI arg (filters/fields) with a friendly error."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        print(f"ERROR: --{name} must be valid JSON, got: {value!r} ({e})", file=sys.stderr)
        raise SystemExit(2)
    return parsed


def cmd_get_record(args) -> int:
    client = _client(args)
    try:
        doc = get_record(client, args.doctype, args.name)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not fetch {args.doctype}/{args.name}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2, default=str))
    return 0


def cmd_list_records(args) -> int:
    client = _client(args)
    filters = _json_arg("filter", args.filter)
    fields = _json_arg("fields", args.fields)
    rows = list_records(
        client, args.doctype, filters=filters, fields=fields,
        limit=args.limit, order_by=args.order_by,
    )
    print(json.dumps(rows, indent=2, default=str))
    print(f"{len(rows)} record(s)", file=sys.stderr)
    return 0


def cmd_set_mapping(args) -> int:
    client = _client(args)
    path = args.overrides or DEFAULT_OVERRIDES

    if args.list:
        data = load_overrides(path)
        block = data.get(args.doctype)
        if not block or not (block.get("mappings") or block.get("defaults")
                             or block.get("value_maps")):
            print(f"No overrides for {args.doctype} in {path}")
            return 0
        print(f"Overrides for {args.doctype} ({path}):")
        for col, tgt in (block.get("mappings") or {}).items():
            print(f"  mapping:  {col} -> {tgt}")
        for f, v in (block.get("defaults") or {}).items():
            print(f"  default:  {f} = {v}")
        for f, m in (block.get("value_maps") or {}).items():
            print(f"  value_map: {f}: {m}")
        return 0

    if args.unset:
        prev_data = load_overrides(path)
        prev = ((prev_data.get(args.doctype) or {}).get("mappings") or {}).get(args.unset)
        ok = unset_mapping(path, args.doctype, args.unset)
        print(f"Removed override for '{args.unset}'" if ok else f"No override for '{args.unset}'")
        if ok:
            sink = _effect_sink(args, args.doctype, source="set-mapping",
                                 command="set-mapping")
            sink.override_set(args.doctype, args.unset, None, prev, path)
            sink.close()
            print(sink.summary())
        return 0

    if not args.column or not args.target:
        print("ERROR: --column and --target are required (or use --unset / --list)",
              file=sys.stderr)
        return 2

    # flat party flow (customers_full|suppliers_full): target is
    # '<party|contact|address>.<fieldname>'
    flow_party = party_for_flow(args.doctype)
    if flow_party:
        parsed = parse_flat_target(args.target)
        if parsed is None:
            allowed = "|".join(sorted({k for k, _ in flat_map_for(flow_party).values()}))
            print(f"ERROR: flat target must be '<doctype>.<fieldname>' where doctype "
                  f"is {allowed} (got {args.target!r}).", file=sys.stderr)
            return 2
        dt_key, field = parsed
        canonical = {"customer": "Customer", "supplier": "Supplier",
                     "contact": "Contact", "address": "Address"}[dt_key]
        parent, children = fetch_with_children(client, canonical)
        valid = {t.qualified for t in MappingEngine(parent, children).targets}
        if field not in valid:
            print(f"ERROR: target field '{field}' is not on {canonical}. "
                  "Use createfield to add it first, or check the fieldname.", file=sys.stderr)
            return 2
        set_mapping(path, args.doctype, args.column, args.target)
        print(f"Override saved: {args.doctype}.{args.column} -> {args.target} ({path})")
        return 0

    # validate the target against live metadata (incl. custom fields)
    parent, children = fetch_with_children(client, args.doctype)
    valid = {t.qualified for t in MappingEngine(parent, children).targets}
    if args.target not in valid:
        print(f"ERROR: target '{args.target}' is not a field on {args.doctype}. "
              "Use createfield to add it first, or check the fieldname.", file=sys.stderr)
        return 2

    prev = ((load_overrides(path).get(args.doctype) or {}).get("mappings") or {}).get(args.column)
    set_mapping(path, args.doctype, args.column, args.target)
    print(f"Override saved: {args.doctype}.{args.column} -> {args.target} ({path})")
    sink = _effect_sink(args, args.doctype, source="set-mapping", command="set-mapping")
    sink.override_set(args.doctype, args.column, args.target, prev, path)
    if hasattr(sink, "config_delta"):
        sink.config_delta(path, args.doctype, {"mappings": {args.column: args.target}})
    sink.close()
    print(sink.summary())
    return 0


def cmd_clean(args) -> int:
    """Flag duplicate keys and empty required values, before mapping or import.

    Needs no target doctype for the key checks (duplicates, blank keys); with
    --doctype it also checks columns that feed required fields, which needs one
    metadata fetch. Exit 2 when any error-severity flag is found, so it can gate
    a pipeline.
    """
    source = read_source(args.source)
    if not _check_id_column(args, source):
        return 2

    if args.doctype:
        client = _client(args)
        parent, children = fetch_with_children(client, args.doctype)
        engine = MappingEngine(parent, children)
        plan = engine.suggest(source)
        key_column = infer_id_column(plan, source, args.id_column)
        key_field = next((m.target for m in plan.mappings
                          if m.source == key_column and m.target), "") or ""
        required = required_mapped_columns(engine.parent, plan)
        compared = [m.source for m in plan.mappings if m.target]
    else:
        key_column = args.id_column or _guess_key_column(source)
        key_field, required, compared = "", [], None

    if key_column is None:
        print("ERROR: cannot determine the key column. Pass --id-column.",
              file=sys.stderr)
        return 2

    conflicts = data_quality_conflicts(
        source, key_column=key_column, key_field=key_field,
        required=required, compare_columns=compared,
    )

    # near duplicates: a review list, so warning-only and never gated
    pairs, pair_count, skipped = possible_duplicate_pairs(
        source, key_column, compare_columns=compared)
    if pair_count:
        conflicts.append(possible_duplicate_row_conflict(
            key_column, pairs, pair_count, key_field, skipped))

    if args.json:
        print(json.dumps(conflicts, indent=2, default=str))
    else:
        how = "explicit" if args.id_column else ("mapped" if args.doctype else "guessed")
        print(f"Data quality — {args.source}  "
              f"({source.n_rows} rows, key column {key_column!r}, {how})")
        if not conflicts:
            print("  no issues found")
        for c in conflicts:
            if c["kind"] == "duplicate_row":
                print(f"  [{c['severity']:<7}] duplicate_row  {c['source']:<20} "
                      f"{c['group_count']} duplicated key(s)")
                for g in c["groups"]:
                    differs = ("  differs: " + ", ".join(g["differing_fields"])
                               if g["differing_fields"] else "  identical rows")
                    flags = "".join([" case" if g["case_variant"] else "",
                                     " whitespace" if g["whitespace_variant"] else ""])
                    print(f"      {g['key_value']!r} rows {g['rows']} "
                          f"({g['count']}x){flags}{differs}")
            elif c["kind"] == "possible_duplicate_row":
                print(f"  [{c['severity']:<7}] possible_dup   {c['source']:<20} "
                      f"{c['pair_count']} pair(s) to review")
                for pr in c["pairs"]:
                    print(f"      row {pr['a']['row']} {pr['a']['value']!r} ~ "
                          f"row {pr['b']['row']} {pr['b']['value']!r}  "
                          f"({', '.join(pr['signals'])})")
                if c.get("signal_a_skipped"):
                    print("      name similarity skipped: sheet exceeds "
                          "MAX_PAIRS_ROWS")
            else:
                roles = ",".join(c["roles"])
                print(f"  [{c['severity']:<7}] missing_value  {c['source']:<20} "
                      f"roles {roles} — {c['count']} row(s): {c['rows']}")

    errors = sum(1 for c in conflicts if c["severity"] == "error")
    if not args.json:
        print(f"  {len(conflicts)} issue(s): {errors} error, "
              f"{len(conflicts) - errors} warning")
    return 2 if errors else 0


def _guess_key_column(source) -> Optional[str]:
    """First column, preferring an obvious ID/Name header. Printed by the caller
    so the guess is visible rather than silent."""
    for header in ("ID", "Name"):
        if source.column_index(header) is not None:
            return header
    return source.headers[0] if source.headers else None


def cmd_describe_doctype(args) -> int:
    client = _client(args)
    try:
        info = describe_doctype(client, args.doctype, include_all=args.all)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not describe {args.doctype}: {e}", file=sys.stderr)
        return 2

    # human summary on stderr, JSON on stdout (agent contract)
    f = info["fields"]
    print(f"{info['doctype']} — {info['field_count']} fields "
          f"(+{info['custom_field_count']} custom), id_field '{info['id_field']}', "
          f"submittable={info['is_submittable']}", file=sys.stderr)
    if f["required"]:
        print(f"  required: {', '.join(x['fieldname'] for x in f['required'])}", file=sys.stderr)
    if f["links"]:
        print(f"  links:    {', '.join(x['fieldname'] + ' -> ' + str(x['doctype']) for x in f['links'])}",
              file=sys.stderr)
    if f["tables"]:
        print(f"  tables:   {', '.join(x['fieldname'] + ' -> ' + str(x['child_doctype']) for x in f['tables'])}",
              file=sys.stderr)
    if f["fetch_from"]:
        print(f"  fetch:    {', '.join(x['fieldname'] + ' <- ' + str(x['fetch_from']) for x in f['fetch_from'])}",
              file=sys.stderr)

    print(json.dumps(info, indent=2, default=str))
    return 0


def cmd_agent(args) -> int:
    """Dispatch `erpgen agent` to the in-package agent."""
    from erpgen.agent import run  # noqa: PLC0415 — keeps CLI startup cheap

    return run(args)


def cmd_delete(args) -> int:
    client = _client(args)
    for name in [n.strip() for n in args.names.split(",") if n.strip()]:
        try:
            client.delete(args.doctype, name)
            print(f"deleted {args.doctype}/{name}")
        except Exception as e:  # noqa: BLE001
            print(f"could not delete {name}: {e}")
    return 0


def _latest_log(doctype: str, prefix: str):
    pat = f"{prefix}{doctype.lower().replace(' ', '-')}-*.jsonl"
    files = sorted(Path("logs").glob(pat))
    return str(files[-1]) if files else None


def _resolve_log(args, prefix: str) -> str:
    """Resolve a log target: --latest <doctype> or an explicit path."""
    if args.latest:
        path = _latest_log(args.latest, prefix)
        if path is None:
            print(f"ERROR: no {prefix}*.jsonl logs for doctype {args.latest!r}",
                  file=sys.stderr)
            raise SystemExit(2)
        return path
    if not args.log:
        print("ERROR: pass a log path or --latest <doctype>", file=sys.stderr)
        raise SystemExit(2)
    return args.log


def cmd_create_record(args) -> int:
    """Create a lookup record from the CLI (journaled like the agent's tool)."""
    client = _client(args)
    fields = _json_arg("fields", args.fields)
    if not isinstance(fields, dict):
        print("ERROR: --fields must be a JSON object, e.g. '{\"item_group_name\": \"Tooling\"}'",
              file=sys.stderr)
        return 2

    ctx = _context(args, args.doctype, command="create-record")
    from erpgen import tools as erpgen_tools  # noqa: PLC0415

    erpgen_tools.ACTIVE_JOURNAL = ctx
    try:
        result = create_record(client, args.doctype, fields)
    finally:
        erpgen_tools.ACTIVE_JOURNAL = None
        if ctx:
            ctx.close()
    print(json.dumps(result, indent=2, default=str))
    if ctx and result.get("created"):
        print(f"Run context: {ctx.path}  ({ctx.effects} effect(s))")
    return 0


def cmd_status(args) -> int:
    """Show a migration context: requirements (pending/satisfied) + effects."""
    target = args.run_log
    if target is None and getattr(args, "latest", None):
        found = latest_run(args.latest, getattr(args, "log_dir", "logs"))
        if found is None:
            print(f"ERROR: no run found for doctype {args.latest!r}", file=sys.stderr)
            return 2
        target = str(found)
    if target is None:
        print("ERROR: pass a run id/path or --latest <doctype>", file=sys.stderr)
        return 2
    try:
        run = resolve_run(target, getattr(args, "log_dir", "logs"))
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    data = load_run(str(run), getattr(args, "log_dir", "logs"))
    eff = data["effects"]
    print(f"Run {data.get('run_id')}  ({data['path']})")
    print(f"  effects applied: {len(eff)}")
    for i, e in enumerate(eff, 1):
        print(f"    #{e.get('seq') or i} {e.get('kind'):<20} "
              f"-> {e.get('inverse', {}).get('op')} {e.get('inverse', {}).get('name', '')}")
    reqs = data["requirements"]
    pending = [r for r in reqs if r.get("satisfied_by") is None]
    print(f"  requirements: {len(reqs)} total, {len(pending)} pending")
    for r in reqs:
        if r.get("satisfied_by") is None and r.get("via") is None:
            state = "PENDING"
        else:
            state = f"satisfied ({r.get('via') or 'effect'})"
        flag = " (REOPENED)" if r.get("reopened") else ""
        print(f"    [{r.get('severity', '?'):<7}] {r.get('kind', ''):<22} {state:<10} "
              f"{(r.get('detail') or '')[:60]}{flag}")
    for c in data["config_delta"]:
        print(f"  config delta: {c.get('doctype')} {c.get('changes')}")
    return 0


def cmd_revert(args) -> int:
    """Revert a migration by replaying its journal's inverses (newest-first).

    Unlike re-deriving intent from import/agent logs, this consumes the effect
    journal written at effect time — so mixed effects (records + custom fields +
    overrides) undo in one command.
    """
    client = _client(args)
    path = None
    if args.log:
        try:
            path = str(resolve_run(args.log, args.log_dir))
        except FileNotFoundError:
            path = args.log
    if path is None:
        run = (latest_run(args.latest, args.log_dir, require_effects=True)
               if args.latest else None)
        path = str(run) if run else _resolve_log(args, "journal-")
    data = parse_journal(path)
    if not data["effects"]:
        print(f"No journaled effects found in {path}")
        return 0

    run = data.get("run_start") or {}
    label = run.get("doctype") or run.get("run_id") or Path(path).stem
    print(f"Revert {label} — {len(data['effects'])} journaled "
          f"effect(s) from {path}")

    res = revert_journal(client, path, apply=args.apply,
                         force=getattr(args, "force", False))
    if res.get("already_reverted"):
        m = res["already_reverted"]
        print(f"Already reverted on {m.get('ts')} ({m.get('reverted')} effect(s)); "
              "nothing to do. Use --force to replay anyway.")
        return 0
    if not args.apply:
        print("Dry run (--apply to execute). Inverses, newest-first:")
        for r in res["results"]:
            print(f"  - {r['description']}")
        return 0

    print(f"Reverted {res['applied']}/{res['total']} effect(s); "
          f"{len(res['failed'])} failed")
    for f in res["failed"]:
        print(f"  FAILED {f['description']}: {f['error'][:160]}")
    return 0 if not res["failed"] else 1


def _add_global_flags(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"ERPNext URL (default {DEFAULT_BASE})")
    ap.add_argument("--user", default="Administrator")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--timeout", type=int, default=300, help="import wait timeout (s)")
    ap.add_argument("--log-dir", default="logs",
                    help="audit log directory (default: logs/)")
    ap.add_argument("--run", metavar="RUN_ID",
                    help="migration run id: all commands sharing it use one context (logs/run-<id>.jsonl), revertible in one step")


def _add_source_parsers(sub) -> None:
    """The two commands that consume a source file."""
    p_map = sub.add_parser("map", help="build a mapping plan from a source file")
    p_map.add_argument("source")
    p_map.add_argument("--doctype", help="target doctype (inferred from headers if omitted)")
    p_map.add_argument("--defaults", help='JSON defaults, e.g. \'{"customer_group":"Commercial"}\'')
    p_map.add_argument("--save", help="save plan JSON to this path")
    p_map.add_argument("--id-column", help="source column carrying the natural key "
                                           "(an explicit value must exist in the sheet)")
    p_map.add_argument("--overrides", help=f"mapping overrides file "
                                          f"(default: {DEFAULT_OVERRIDES} if present)")
    p_map.add_argument("--analysis-dir", default="analysis",
                       help="where to write the agent-consumable analysis JSON "
                            "(default: analysis/)")
    p_map.set_defaults(fn=cmd_map)

    p_imp = sub.add_parser(
        "import",
        help="map + idempotent import (dry-run unless --apply; duplicates skipped, "
             "everything logged)",
    )
    p_imp.add_argument("source")
    p_imp.add_argument("--doctype", help="target doctype (auto-detected for flat customers_full sheets)")
    p_imp.add_argument("--defaults")
    p_imp.add_argument("--apply", action="store_true", help="actually run the import")
    p_imp.add_argument("--bulk", action="store_true",
                       help="use the Data Import machinery instead of per-record REST")
    p_imp.add_argument("--submit", action="store_true", help="submit docs after creation")
    p_imp.add_argument("--id-column", help="source column carrying the natural key "
                                           "(auto-inferred when possible)")
    p_imp.add_argument("--bypass-conflicts", action="store_true",
                       help="import even if error-severity conflicts remain "
                            "(default: fail before importing partial data)")
    p_imp.add_argument("--overrides", help=f"mapping overrides file "
                                          f"(default: {DEFAULT_OVERRIDES} if present)")
    p_imp.add_argument("--analysis-dir", default="analysis",
                       help="where to write the agent-consumable analysis JSON "
                            "(default: analysis/)")
    p_imp.set_defaults(fn=cmd_import)


def _add_definition_parsers(sub) -> None:
    """Commands that inspect or extend a doctype's schema."""
    p_cf = sub.add_parser(
        "createfield",
        help="create a custom field (column) on a doctype, e.g. to hold an "
             "unmapped source column",
    )
    p_cf.add_argument("doctype", help="doctype to extend, e.g. Customer")
    p_cf.add_argument("--label", help="human-readable label (required)")
    p_cf.add_argument("--fieldname", help="snake_case fieldname (auto-derived from label)")
    p_cf.add_argument("--fieldtype", default="Data",
                      help="Data|Int|Currency|Date|Select|Link|Check|... (default Data)")
    p_cf.add_argument("--options", help="Select: comma list; Link: target doctype")
    p_cf.add_argument("--reqd", action="store_true")
    p_cf.add_argument("--read-only", action="store_true", dest="read_only")
    p_cf.add_argument("--insert-after", help="place after this existing fieldname")
    p_cf.add_argument("--fetch-from", help="e.g. customer_primary_contact.email_id")
    p_cf.add_argument("--default", help="default value")
    p_cf.add_argument("--list", action="store_true",
                      help="list existing custom fields on the doctype instead")
    p_cf.set_defaults(fn=cmd_createfield)

    p_dd = sub.add_parser(
        "describe-doctype",
        help="summarize a doctype's structure (required fields, links, child "
             "tables, fetch_from) so the agent can construct records",
    )
    p_dd.add_argument("doctype")
    p_dd.add_argument("--all", action="store_true", help="include the full field list")
    p_dd.set_defaults(fn=cmd_describe_doctype)


def _add_query_parsers(sub) -> None:
    """Read-only lookups plus the mapping-override writer."""
    p_gr = sub.add_parser("get-record", help="fetch a single record as JSON")
    p_gr.add_argument("doctype")
    p_gr.add_argument("name")
    p_gr.set_defaults(fn=cmd_get_record)

    p_lr = sub.add_parser("list-records", help="list records as JSON (existence checks, options)")
    p_lr.add_argument("doctype")
    p_lr.add_argument("--filter", help='ERPNext filters JSON, e.g. \'[["name","=","Wholesale"]]\' '
                                       "(also supports like filters)")
    p_lr.add_argument("--fields", help='fields JSON, e.g. \'["name","customer_group"]\' '
                                       "(default: [name])")
    p_lr.add_argument("--limit", type=int, default=0, help="0 = all (default)")
    p_lr.add_argument("--order-by", default=None)
    p_lr.set_defaults(fn=cmd_list_records)

    p_sm = sub.add_parser(
        "set-mapping",
        help="record a forced source-column -> target-field mapping override "
             "(fixes ambiguous/missed mappings; consumed by map/import)",
    )
    p_sm.add_argument("doctype")
    p_sm.add_argument("--column", help="source column name")
    p_sm.add_argument("--target", help="target field (qualified for child tables, "
                                       "e.g. items.item_code)")
    p_sm.add_argument("--overrides", help=f"overrides file (default: {DEFAULT_OVERRIDES})")
    p_sm.add_argument("--unset", metavar="COLUMN", help="remove a mapping override")
    p_sm.add_argument("--list", action="store_true", help="show current overrides")
    p_sm.set_defaults(fn=cmd_set_mapping)


def _add_lifecycle_parsers(sub) -> None:
    """Create lookup records, inspect a run, revert a run, delete records."""
    p_cr = sub.add_parser(
        "create-record",
        help="create a lookup record (Item Group, UOM, ...); journaled, and shares "
             "the migration context when --run is given",
    )
    p_cr.add_argument("doctype")
    p_cr.add_argument("--fields", required=True,
                      help='JSON object, e.g. \'{"item_group_name": "Tooling"}\'')
    p_cr.set_defaults(fn=cmd_create_record)

    p_st = sub.add_parser(
        "status",
        help="show a migration context: effects applied + requirements pending/satisfied",
    )
    p_st.add_argument("run_log", nargs="?", metavar="run",
                      help="run id or path to logs/run-*.jsonl")
    p_st.add_argument("--latest", metavar="DOCTYPE",
                      help="newest run touching this doctype")
    p_st.set_defaults(fn=cmd_status)

    p_rv = sub.add_parser(
        "revert",
        help="revert a migration by replaying its journal's inverses (newest-first). "
             "Consumes logs/journal-<doctype>-<ts>.jsonl — undoes records, custom "
             "fields and overrides in one command",
    )
    p_rv.add_argument("log", nargs="?", metavar="journal",
                      help="path to a logs/journal-*.jsonl file")
    p_rv.add_argument("--latest", metavar="DOCTYPE",
                      help="use the newest journal for this doctype instead of a path")
    p_rv.add_argument("--apply", action="store_true",
                      help="actually execute the inverses (default: dry-run preview)")
    p_rv.add_argument("--force", action="store_true",
                      help="replay a journal that was already fully reverted")
    p_rv.set_defaults(fn=cmd_revert)

    p_cl = sub.add_parser(
        "clean",
        help="flag duplicate keys and empty required values before mapping/import "
             "(exit 2 when any error is found)",
    )
    p_cl.add_argument("source")
    p_cl.add_argument("--doctype", help="also check columns feeding required fields "
                                       "(one metadata fetch); omit for key checks only")
    p_cl.add_argument("--id-column", help="source column carrying the natural key "
                                         "(an explicit value must exist in the sheet)")
    p_cl.add_argument("--json", action="store_true", help="emit the conflicts as JSON")
    p_cl.set_defaults(fn=cmd_clean)

    p_del = sub.add_parser("delete", help="delete records by name (cleanup)")
    p_del.add_argument("--doctype", required=True)
    p_del.add_argument("--names", required=True, help="comma-separated names")
    p_del.set_defaults(fn=cmd_delete)

    p_ag = sub.add_parser(
        "agent",
        help="LLM agent: resolve mapping conflicts, then import (imports "
             "deterministically first when there is nothing to decide)",
    )
    add_agent_flags(p_ag)
    p_ag.set_defaults(fn=cmd_agent)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Extracted from main() so tests can parse argv
    without dispatching a command."""
    ap = argparse.ArgumentParser(prog="erpgen.py", description=__doc__)
    _add_global_flags(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_source_parsers(sub)
    _add_definition_parsers(sub)
    _add_query_parsers(sub)
    _add_lifecycle_parsers(sub)
    return ap




def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    try:
        return args.fn(args)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
