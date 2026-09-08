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

from erpgen.analysis import build_analysis, save_analysis  # noqa: E402
from erpgen.client import ERPNextClient  # noqa: E402
from erpgen.dedup import (  # noqa: E402
    dedup_payloads,
    existing_names,
    infer_id_column,
    resolve_key_field,
)
from erpgen.loader import DataImportLoader, RestLoader  # noqa: E402
from erpgen.logger import RunLogger  # noqa: E402
from erpgen.mapper import MappingEngine  # noqa: E402
from erpgen.metadata import DoctypeMeta, fetch_with_children  # noqa: E402
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

DEFAULT_BASE = "http://localhost:8082"


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
        id_column=infer_id_column(plan, source, None),
        base_url=args.base, source_path=args.source,
    )
    apath = save_analysis(analysis, args.analysis_dir)
    print(f"Analysis saved to {apath}  "
          f"({len(analysis['conflicts'])} conflicts, "
          f"{len(analysis['suggested_custom_fields'])} suggested custom fields)")
    return 0


def cmd_import(args) -> int:
    source = read_source(args.source)
    engine, client = _engine(args, args.doctype)
    plan = engine.suggest(source)
    o_path, overrides = _overrides_for(args, plan.doctype)
    if overrides:
        n = apply_overrides(plan, source, engine, overrides)
        if n:
            print(f"Applied {n} mapping override(s) from {o_path}")
    _print_plan(plan, source)

    payloads, row_errors = engine.build_payloads(source, plan)
    if row_errors:
        print(f"\n{len(row_errors)} rows failed value conversion:")
        for e in row_errors[:10]:
            print(f"  row {e['row']}: {e['errors']}")
    if plan.fetch_from_conflicts:
        print("NOTE: fetch_from fields were dropped from payloads (see conflicts above)")

    # ---- idempotency setup ----
    id_column = infer_id_column(plan, source, args.id_column)
    if id_column is None:
        print(f"\nERROR: cannot determine the ID column for {plan.doctype} "
              f"(id field: {plan.id_field!r}). Pass --id-column <source column>.")
        return 2
    if args.id_column:
        _inject_id_column(source, payloads, plan, args.id_column)

    key_field = resolve_key_field(payloads, plan.id_field or "name")
    print(f"\nPrepared {len(payloads)} payloads for {plan.doctype}")
    print(f"  id_field: {plan.id_field!r} | key field: {key_field!r} | id_column: {id_column!r}")

    # ---- agent-consumable analysis artifact (saved on dry-run AND apply) ----
    analysis = build_analysis(
        client, source, plan, engine,
        id_column=id_column, base_url=args.base, source_path=args.source,
    )
    apath = save_analysis(analysis, args.analysis_dir)
    print(f"Analysis saved to {apath}  "
          f"({len(analysis['conflicts'])} conflicts, "
          f"{len(analysis['suggested_custom_fields'])} suggested custom fields)")

    # ---- run log (created before any write so conversion failures are logged) ----
    mode = "bulk (Data Import)" if args.bulk else "upsert (REST)"
    logger = None
    if args.apply:
        logger = RunLogger(args.log_dir, tag=f"import-{plan.doctype.lower().replace(' ', '-')}")
        logger.run_start(
            doctype=plan.doctype,
            source=args.source,
            base=args.base,
            mode=mode,
            id_field=plan.id_field,
            key_field=key_field,
            id_column=id_column,
            submit=args.submit,
            defaults=plan.defaults,
            plan=plan.as_dict(),
        )
        for e in row_errors:
            logger.row(e["row"], "", "failed", message="; ".join(e["errors"]))

    # ---- predicted dedup (read-only, safe in dry-run too) ----
    keys = [str(p.get(key_field) or "").strip() for p in payloads if p.get(key_field)]
    existing = existing_names(client, plan.doctype, plan.id_field or "name", keys) if keys else set()
    predicted_new = [k for k in keys if k not in existing]
    print(f"  of {len(keys)} keyed rows: {len(predicted_new)} new, "
          f"{len(keys) - len(predicted_new)} already exist (will be skipped)")

    if not args.apply:
        print("\nDry run (use --apply to import). ")
        if args.bulk:
            csv_text = engine.build_template_csv(plan, payloads)
            print("CSV preview (bulk path):")
            print("\n".join(csv_text.splitlines()[:6]))
        else:
            print("First payload (REST upsert path):")
            print(json.dumps({k: v for k, v in payloads[0].items() if k != "__row"},
                             indent=2) if payloads else "{}")
        return 0

    to_create, skipped = dedup_payloads(
        client, plan.doctype, plan, payloads, id_column=id_column, logger=logger
    )
    print(f"\nDedup: {len(to_create)} to create, {len(skipped)} skipped")

    if args.bulk:
        if not to_create:
            print("Nothing new to import; skipping Data Import run.")
        else:
            csv_text = engine.build_template_csv(plan, to_create)
            result = DataImportLoader(client).load(
                plan.doctype,
                csv_text,
                import_type="Insert New Records",
                submit_after_import=args.submit,
                skipped=len(skipped),
                timeout=args.timeout,
                logger=logger,
            )
            print(json.dumps(result.as_dict(), indent=2))
    else:
        results = RestLoader(client).upsert(
            plan.doctype,
            to_create,
            key_field,
            existing=existing,
            submit=args.submit,
            logger=logger,
        )
        ok = sum(1 for r in results if r.ok and not r.skipped)
        print(f"REST upsert: created {ok}, failed {sum(1 for r in results if not r.ok)}")

    # ---- post-run verification ----
    created_keys = [str(p.get(key_field)) for p in to_create if p.get(key_field)]
    verified = existing_names(client, plan.doctype, plan.id_field or "name",
                              [k for k in created_keys if k])
    logger.run_end(
        verified_created=len(verified),
        new_keys=len(created_keys),
    )
    print()
    print(logger.summary(plan.doctype, args.source))
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
        ok = unset_mapping(path, args.doctype, args.unset)
        print(f"Removed override for '{args.unset}'" if ok else f"No override for '{args.unset}'")
        return 0

    if not args.column or not args.target:
        print("ERROR: --column and --target are required (or use --unset / --list)",
              file=sys.stderr)
        return 2

    # validate the target against live metadata (incl. custom fields)
    parent, children = fetch_with_children(client, args.doctype)
    valid = {t.qualified for t in MappingEngine(parent, children).targets}
    if args.target not in valid:
        print(f"ERROR: target '{args.target}' is not a field on {args.doctype}. "
              "Use createfield to add it first, or check the fieldname.", file=sys.stderr)
        return 2

    set_mapping(path, args.doctype, args.column, args.target)
    print(f"Override saved: {args.doctype}.{args.column} -> {args.target} ({path})")
    return 0


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


def cmd_delete(args) -> int:
    client = _client(args)
    for name in [n.strip() for n in args.names.split(",") if n.strip()]:
        try:
            client.delete(args.doctype, name)
            print(f"deleted {args.doctype}/{name}")
        except Exception as e:  # noqa: BLE001
            print(f"could not delete {name}: {e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="erpgen.py", description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"ERPNext URL (default {DEFAULT_BASE})")
    ap.add_argument("--user", default="Administrator")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--timeout", type=int, default=300, help="import wait timeout (s)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_map = sub.add_parser("map", help="build a mapping plan from a source file")
    p_map.add_argument("source")
    p_map.add_argument("--doctype", required=True)
    p_map.add_argument("--defaults", help='JSON defaults, e.g. \'{"customer_group":"Commercial"}\'')
    p_map.add_argument("--save", help="save plan JSON to this path")
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
    p_imp.add_argument("--doctype", required=True)
    p_imp.add_argument("--defaults")
    p_imp.add_argument("--apply", action="store_true", help="actually run the import")
    p_imp.add_argument("--bulk", action="store_true",
                       help="use the Data Import machinery instead of per-record REST")
    p_imp.add_argument("--submit", action="store_true", help="submit docs after creation")
    p_imp.add_argument("--id-column", help="source column carrying the natural key "
                                           "(auto-inferred when possible)")
    p_imp.add_argument("--log-dir", default="logs", help="audit log directory (default: logs/)")
    p_imp.add_argument("--overrides", help=f"mapping overrides file "
                                          f"(default: {DEFAULT_OVERRIDES} if present)")
    p_imp.add_argument("--analysis-dir", default="analysis",
                       help="where to write the agent-consumable analysis JSON "
                            "(default: analysis/)")
    p_imp.set_defaults(fn=cmd_import)

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

    p_del = sub.add_parser("delete", help="delete records by name (cleanup)")
    p_del.add_argument("--doctype", required=True)
    p_del.add_argument("--names", required=True, help="comma-separated names")
    p_del.set_defaults(fn=cmd_delete)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
