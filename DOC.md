# erpgen — agentic Excel/ERP → ERPNext migration tooling

Converts messy SME spreadsheets (CSV / XLSX) into ERPNext, using the **live
target site's DocType metadata** as the ground truth for mapping.

## Quick start

```bash
# zero-dependency core; openpyxl optional for .xlsx (venv provided)
# --doctype is optional: the tool infers it from the source headers
python3 erpgen.py map samples/customers.csv \
    --defaults '{"customer_group":"Commercial","territory":"All Territories"}'

# dry-run import (plan + payloads + predicted dedup, touches nothing)
python3 erpgen.py import samples/customers.csv
python3 erpgen.py import samples/customers.csv \
    --defaults '{"customer_group":"Commercial","territory":"All Territories"}'

# idempotent import: creates only NEW records, skips duplicates, logs everything.
# By default it FAILS (exit 2) before applying if error-severity conflicts remain;
# --bypass-conflicts restores the original partial-import behavior.
python3 erpgen.py import samples/customers.csv --apply
python3 erpgen.py import samples/customers.csv \
    --defaults '{"customer_group":"Commercial","territory":"All Territories"}' --apply

# flat "SMB" sheet: contacts/addresses inline in the same file, auto-detected by
# header (no --doctype); one Customer + Contact + Address per row, deduped
python3 erpgen.py import samples/customers-smb.csv            # dry run
python3 erpgen.py import samples/customers-smb.csv --apply

# bulk path via the Data Import machinery (also deduped), optional submit
python3 erpgen.py import samples/customers.csv --apply --bulk --submit

# explicit natural-key column when it isn't auto-inferred
python3 erpgen.py import samples/sales_orders.csv --doctype "Sales Order" \
    --id-column "Sales Order ID" --apply

# cleanup
python3 erpgen.py delete --doctype Customer --names "Acme Steel Works"
```

Defaults: `--base http://localhost:8082 --user Administrator --password admin`.
For `.xlsx` use the venv: `.venv/bin/python erpgen.py ...` (see below).

## Doctype inference & flat SMB sheets

`map` and `import` **infer the doctype** when `--doctype` is omitted
(`erpgen/infer.py`), using two signals in priority order:

1. **Header identity columns** (content beats naming): a strong column such as
   `Customer Name`/`Customer`, `Supplier Name`/`Supplier`, `Item Code`/`Item
   Name`, `First Name`/`Contact Name`, or `Address Title`/`Address Line 1`
   names the target.
2. **File-name prefix** (naming convention): `customers*.csv` → Customer,
   `items*.csv` → Item, `addresses*.csv` → Address, `contacts*.csv` → Contact
   (case-insensitive basename match, e.g. `Items_export.csv`).

Inference is a convenience — pass `--doctype` explicitly when both signals are
absent, and the tool errors (exit 2) rather than guess when it can't tell.

A **flat SMB sheet** — where contacts/addresses are inline columns in the same
file (`Customer Name, Customer Type, Group, Territory, Contact Name, Email,
Phone, Address Type, Address Line 1, City, State, Postal Code, Country`) — is
detected by header and routed through the normal `import` command to the
customers_full flow: one Customer + linked Contact + Address per row, all
idempotent. No `--doctype` is required; `--defaults` still applies (e.g.
Group/Territory). A **contact shared across customers** (same email) is created
once and gets a `Dynamic Link` row per customer (`linked` in the summary)
instead of being re-created or silently dropped; addresses stay one-per-customer.
Per-row insert failures (e.g. a required `address_line1`/`city`/`country`
missing) are logged as `failed` with a `WARNING:` on stderr and skipped — they
never abort the run. A failed record is **not** added to its dedup set, so
fixing the source and re-running retries it and (for addresses/contacts) links
it to its customer. Applied runs log to `logs/customers_full-<ts>.jsonl` (one
`row` event per doctype), which is **not** yet consumed by `rollback` (that
reads `logs/import-*.jsonl`).

### Out-of-contract columns & the flat contract override

Columns **not** in the fixed `FLAT_MAP` contract (e.g. `Tax ID`, `Website`,
`Loyalty Tier`, `Customer Since`) are surfaced by `map`/`import` as
error-severity `unmapped_column` conflicts in a `customers_full` analysis,
each with a `suggested_action`: map it to an existing field, or create a custom
field then map it. Resolve them by extending the flat contract with `set-mapping`:

```bash
# maps to an existing field
python3 erpgen.py set-mapping customers_full --column "Tax ID" --target customer.tax_id

# needs a new field first
python3 erpgen.py createfield Customer --label "Customer Since" --fieldtype Date
python3 erpgen.py set-mapping customers_full --column "Customer Since" --target customer.customer_since
```

`set-mapping customers_full` targets are `customer|contact|address.<fieldname>`
and are validated against live metadata. Once a column is mapped, it leaves the
conflict list and its values import into the right doctype (rerun `map` to
confirm zero conflicts).

## Conflict gate (fail-before-apply)

By default `import --apply` **refuses to run while error-severity conflicts
remain** (missing required fields, link values that don't exist on the target,
etc.), printing them and exiting 2. Resolve them (`set-mapping` /
`createfield` / `create-record`), or pass `--bypass-conflicts` to import the
conflict-free subset anyway. Dry-run (no `--apply`) is never gated — it always
shows the plan + predicted dedup. The agent's `run_import` tool inherits this:
it must resolve conflicts before it can apply.

## Idempotency & logging

Imports are **idempotent**: before inserting anything the tool queries which
natural keys already exist on the target site, creates **only new records**, and
**skips duplicates** (logged as `skipped`, never re-created, never errored).
The natural key is the doctype's autoname field (e.g. `customer_name`, `item_code`)
or `name`; it's auto-inferred from the live metadata and the mapping
(`--id-column` overrides). Re-running the same source is a safe no-op.

**Everything is logged**: each `--apply` run appends an audit trail to
`logs/import-<doctype>-<timestamp>.jsonl` (JSONL, one event per line):
- `run_start` — doctype, source, mode, id field/column, defaults, full plan
- `row` — one line per source row: source row number, key, status
  (`created` | `skipped` | `failed`), docname, message
- `run_end` — totals + duration + post-run verification count
- bulk path adds `data_import_start` / `data_import_end` with the job name and
  file reference

`--log-dir` changes the log location (default `logs/`). A human summary is
printed after every run.

## What the mapper does

1. **Profiles the source** — per-column type inference, emptiness, uniqueness,
   messiness flags (currency symbols, comma-as-decimal, whitespace).
2. **Discovers the target** — pulls `DocType` metadata from the live site:
   fieldtype, `reqd`, `read_only`, `fetch_from`, Link options, Table children.
3. **Suggests mappings** — exact/normalized/token/fuzzy label+fieldname matching
   plus a synonym table; scores every column; flags ambiguity.
4. **Flags the traps** (all discovered the hard way against the demo site):
   - `fetch_from` (read-only) fields — e.g. `Customer.email_id` is fetched from
     the primary Contact; direct writes are silently discarded. The mapper
     drops them from payloads and tells you to write the source doc.
   - Required fields with no source and no default.
   - Link fields whose values must exist in the target (e.g. Item Group, UOM).
   - Child-table columns (e.g. `items.*`, `credit_limits.credit_limit`) with the
     exact Data Import header required, e.g. `Credit Limit (Credit & Overdue Limits)`.
5. **Builds payloads / template CSV** — value conversion (dates → ISO,
   currencies → float, booleans), applies defaults, groups child rows.

## Architecture

```
erpgen/
  client.py    ERPNextClient — REST + Data Import + metadata (stdlib urllib)
  metadata.py  DoctypeMeta/FieldMeta models from live DocType docs
  source.py    CSV/XLSX readers + column profiling
  mapper.py    MappingEngine → MappingPlan (JSON-serializable, LLM-slot ready)
  dedup.py     natural-key resolution + existence checks (idempotency)
  logger.py    RunLogger — JSONL audit trail + run summary
  loader.py    RestLoader.upsert (default) + DataImportLoader (--bulk)
erpgen.py     CLI: map | import | createfield | describe-doctype | get-record |
              list-records | set-mapping | rollback | rollback-schema |
              rollback-options | delete
samples/       demo CSV + XLSX
logs/          per-run audit logs (JSONL)
```

Deterministic for now; `MappingEngine.suggest(llm=...)` accepts a callback for
LLM-assisted disambiguation of hard cases later.

## Mapping analysis artifact (for LLM agents)

`map` and `import` (dry-run and apply) save an **agent-consumable analysis** to
`analysis/analysis-<doctype>-<timestamp>.json` (`--analysis-dir` to relocate):

```json
{
  "schema_version": 1,
  "doctype": "Customer",
  "source": "samples/customers.csv",
  "source_rows": 9,
  "id_field": "name",
  "id_column": "Customer Name",
  "base_url": "http://localhost:8082",
  "plan": { "mappings": [ {"source", "target", "confidence", "method",
                           "notes", "alternatives"} ], "defaults", "warnings",
            "fetch_from_conflicts", "link_fields", "id_field" },
  "column_profiles": [ {"header", "inferred_type", "non_empty", "unique",
                        "sample", "messy"} ],
  "conflicts": [
    {"kind": "unmapped_column",    "severity": "info",    "source": "Notes", ...},
    {"kind": "ambiguous_mapping",  "severity": "warning", "source": "Group",
     "target": "customer_group", "alternatives": ["tax_withholding_group"], ...},
    {"kind": "fetch_from",         "severity": "warning", "target": "email_id",
     "fetch_from": "customer_primary_contact.email_id", ...},
    {"kind": "required_missing",   "severity": "error",   "field": "customer_type", ...},
    {"kind": "link_value_conflict","severity": "error",   "target": "customer_group",
     "doctype": "Customer Group", "missing_values": ["Wholesale"], ...}
  ],
  "suggested_custom_fields": [
    {"source": "Vendor Code", "fieldname": "vendor_code", "fieldtype": "Data",
     "create_command": "python3 erpgen.py createfield Customer --label 'Vendor Code' --fieldtype Data"}
  ],
  "agent_instructions": "You are the migration-fix agent ... act on the conflicts ..."
}
```

A downstream agent reads this file, resolves each conflict (run
`suggested_custom_fields[].create_command` for unmapped columns, create missing
option records for `link_value_conflict`, pick targets for `ambiguous_mapping`,
supply `--defaults` for `required_missing`), then re-runs
`python3 erpgen.py import ... --apply` — the analysis regenerates each run, so
progress is visible as conflicts shrink.

## LLM agent skeleton (LlamaIndex AgentWorkflow)

`scripts/agent.py` is the agent skeleton: it reads the latest analysis, then an
LLM agent resolves conflicts via tools and imports — looping until
error-severity conflicts are zero.

```bash
.venv/bin/python scripts/agent.py --doctype Customer --source samples/customers.csv \
    --defaults '{"customer_group":"Commercial"}'            # fresh analysis + agent run
.venv/bin/python scripts/agent.py --analysis analysis/analysis-customer-<ts>.json  # resume
.venv/bin/python scripts/agent.py --doctor --doctype Customer --source samples/customers.csv
#   ^ no-LLM mode: prints the 9 tools + current conflicts, for wiring/debugging

# flat SMB sheet (Customer + Contact + Address): full agentic loop — the LLM
# resolves out-of-contract columns (create fields + set_mapping) then imports
.venv/bin/python scripts/agent.py --source samples/customers-smb.csv
```

`--doctype` is optional; the agent infers it from the source headers like the
CLI does (a flat SMB sheet is handled as doctype `customers_full`). Provider:
`--provider auto|openai|deepseek` (auto-detected from `OPENAI_API_KEY` /
`DEEPSEEK_API_KEY`; DeepSeek defaults to model `deepseek-v4-flash` at
`https://api.deepseek.com`, overridable with `--model` / `--api-base`).

Tools the agent can call: `latest_analysis`, `run_map`, `run_import`,
`create_field`, `create_record`, `set_mapping`, `describe_doctype`,
`get_record`, `list_records` — each wraps the same functions the CLI uses
(in-process), so the agent and the CLI can never drift apart.

Note: `create_field`/`create_record` were extracted into `erpgen/tools.py`
(reused by the `createfield` CLI command) so the agent calls real shared code.

## Creating fields programmatically

Unmapped source columns are dropped with a warning — to keep that data, give it
a home first with `createfield` (creates a `Custom Field`, i.e. a real column,
via the REST API; the mapper auto-discovers it on the next run):

```bash
python3 erpgen.py createfield Customer --label "Vendor Code" --fieldtype Data
python3 erpgen.py createfield Customer --label "Customer Tier" --fieldtype Select \
    --options "Standard,Premium,Enterprise" --insert-after customer_group
python3 erpgen.py createfield Customer --list          # see what exists
```

- `--fieldname` is auto-derived from the label (`Vendor Code` → `vendor_code`),
  or set it explicitly; `--reqd`, `--read-only`, `--default`, `--fetch-from`
  also supported.
- Idempotent: re-running with an existing fieldname reports "already exists".
- Delete a field via `DELETE /api/resource/Custom Field/<dt>-<fieldname>`
  (drops the column).

## Record inspection (agent verification)

Agent-friendly read tools — JSON on stdout (stderr carries human notes/errors):

```bash
python3 erpgen.py get-record Customer "Acme Steel Works"
python3 erpgen.py list-records "Customer Group"                        # all, name only
python3 erpgen.py list-records Customer --filter '[["customer_group","=","Commercial"]]' \
    --fields '["name","tax_id"]'
python3 erpgen.py list-records "Customer Group" --filter '[["name","like","%Commercial%"]]'
```

`get-record` exits 2 with a clear error when the record doesn't exist —
machine-detectable for the agent loop.

`describe-doctype` tells the agent how to construct records — required fields,
Link targets, child tables, fetch_from fields, the id field, and custom-field
count (JSON on stdout, human summary on stderr):

```bash
python3 erpgen.py describe-doctype Customer                 # summary categories
python3 erpgen.py describe-doctype Item --all               # + full field list
python3 erpgen.py describe-doctype "Customer Group"         # id_field=customer_group_name
```

This is what lets `create-record` (next tool) know that a Customer Group needs
`customer_group_name`, not `name`.

## Mapping overrides (`set-mapping`)

Record a deliberate decision — "this source column maps to that target field" —
instead of editing the source file. Fixes `ambiguous_mapping` conflicts and
missed/wrong auto-mappings:

```bash
python3 erpgen.py set-mapping Customer --column "Group" --target customer_group
python3 erpgen.py set-mapping Customer --column "Item" --target items.item_code   # child table
python3 erpgen.py set-mapping Customer --list       # show overrides
python3 erpgen.py set-mapping Customer --unset "Group"
```

Writes `mapping-overrides.json`:

```json
{ "Customer": {
    "mappings":  {"Group": "customer_group"},
    "defaults":  {"customer_type": "Company"},            // future: set-default
    "value_maps": {"customer_group": {"Wholesale": "Commercial"}}  // future: set-value-map
} }
```

`map`/`import` auto-load this file (or `--overrides <file>` for per-client
configs) and apply it **after** scoring: forced targets win (method becomes
`override`), defaults fill missing required fields, value_maps remap values
before insert. Invalid targets are rejected against live metadata; unknown
source columns are ignored with a warning. The source file is never touched —
decisions live in the JSON and show up in every analysis artifact.

## Rollback (undo a migration)

Every import and agent run writes logs, so migrations can be **rolled back**.
Three commands, each dry-run by default (`--apply` to execute), each accepting
an explicit `log` path or `--latest <doctype>` to pick the newest matching log:

| Command | Undoes | Log consumed |
|---|---|---|
| `rollback` | records created by an import run (newest-first) | `logs/import-<doctype>-*.jsonl` |
| `rollback-options` | lookup records the agent created (Item Groups, UOMs, Territories…) | `logs/agent-<doctype>-*.jsonl` |
| `rollback-schema` | custom fields the agent created — **drops the columns + data** | `logs/agent-<doctype>-*.jsonl` |

```bash
# preview (default)
python3 erpgen.py rollback --latest Item
python3 erpgen.py rollback-options --latest Item
python3 erpgen.py rollback-schema --latest Item

# execute
python3 erpgen.py rollback --latest Item --apply
python3 erpgen.py rollback-options --latest Item --apply
python3 erpgen.py rollback-schema --latest Item --apply   # destructive
```

Full undo of an agent run (records + option records + fields):

```bash
python3 erpgen.py rollback --latest Item --apply
python3 erpgen.py rollback-options --latest Item --apply
python3 erpgen.py rollback-schema --latest Item --apply
```

Behavior notes:
- `rollback`/`rollback-options` report (rather than abort on) failures — e.g. a
  record now referenced by a transaction won't delete and is listed as `FAILED`.
- `rollback-schema` is **irreversible for column data**; it only drops fields
  the agent actually created (`created: true` in the transcript), never fields
  it merely found existing.
- Rollback is scoped to **one run's log** — records from other runs are
  untouched, so you can unwind migrations independently.
- Flat SMB-sheet imports write `logs/customers_full-*.jsonl` and are **not**
  covered by `rollback` (see "Doctype inference & flat SMB sheets").

## Verified against the demo (v16)

- Customers CSV → plan → import: created 4, **re-run → 0 created, 4 skipped**,
  incl. child credit limits and fetch_from handling.
- Bulk path (Data Import): deduped CSV → 1 created + 4 skipped, per-row audit log.
- XLSX source: same pipeline.
- Sales Order template: parent + child `items` mapping, required-field report.
- Rollback trio: records / option records / custom fields each dry-run + `--apply`
  verified against throwaway Item + Customer Group data; `--latest` resolves
  the newest log per doctype.
- Known limitation: multi-row parents (one Sales Order spread over several
  source rows) are not yet grouped — each source row becomes one document.

## Local demo stack

See `docker/README.md` (ERPNext v16 on :8082, OrbStack, setup wizard note).
