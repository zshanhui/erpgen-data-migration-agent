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

# flat "SMB" party sheets: contacts/addresses inline in the same file,
# auto-detected by header (no --doctype); one party + Contact + Address per row
python3 erpgen.py import samples/customers-smb.csv            # dry run
python3 erpgen.py import samples/customers-smb.csv --apply
python3 erpgen.py import samples/suppliers-smb.csv --apply    # Supplier flow
# ^ a contact/address may be shared across parties (Acme is both customer+supplier)

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

## Full agentic run — every master worksheet

`scripts/run-all-agentic.sh` drives the LLM loop over all supported master
sheets in dependency order (parties → the Contact/Address sheets that link to
them → items). Overlaps are safe: imports are idempotent.

```bash
export DEEPSEEK_API_KEY=...                 # the agent needs an LLM
cd data-migration

./scripts/run-all-agentic.sh                # LLM resolves conflicts, then imports
DOCTOR=1 ./scripts/run-all-agentic.sh       # no LLM: build each map + show conflicts
RUN_ID=myrun ./scripts/run-all-agentic.sh   # name the run so it reverts as one unit
```

| # | Worksheet | Flow / doctype |
|---|---|---|
| 1 | `customers-smb.csv` | `customers_full` — Customer + Contact + Address |
| 2 | `customers.csv` | Customer (relational) |
| 3 | `customers_e2e.csv` | Customer (conflict-rich) |
| 4 | `customers.xlsx` | Customer (xlsx reader path) |
| 5 | `suppliers-smb.csv` | `suppliers_full` — Supplier + Contact + Address |
| 6 | `contacts.csv` | Contact (links to the customers above) |
| 7 | `addresses.csv` | Address (links to the customers above) |
| 8 | `items.csv` | Item |
| 9 | `items_e2e.csv` | Item (conflict-rich) |

Run one sheet at a time the same way:

```bash
.venv/bin/python scripts/agent.py --source samples/suppliers-smb.csv \
    --provider deepseek --max-rounds 20 --run myrun
```

Notes:
- Run from `data-migration/`, and use `.venv/bin/python` — the agent needs
  llama-index and `.xlsx` needs openpyxl. `python3` alone will not work.
- `--run <id>` joins one unified context (`logs/run-<id>.jsonl`) so the mapper's
  conflicts become the run's requirements and every effect is revertible
  together. Without it the agent writes a per-doctype journal instead.
- The agent imports (with `apply`) once error-severity conflicts reach zero —
  there is no separate `--apply`.
- Same two commands to inspect/undo the whole run:
  `python3 erpgen.py status <id>` and `python3 erpgen.py revert <id> --apply`
  (revert previews unless `--apply`).
- `sales_orders.csv` is **transactional**, not master data — it needs Customers
  and Items to exist first.

### Troubleshooting `openai.APIConnectionError: Connection error.`

The OpenAI SDK raises the *same* `APIConnectionError` for DNS failure, TLS
verification errors, a dead proxy and a refused connection — hence a traceback
with nothing actionable in it. The agent therefore probes the endpoint before the
loop, classifies any failure by HTTP status and shows a help block:

```
LLM call failed — APIConnectionError: Connection error.

  endpoint : https://api.deepseek.com
  model    : deepseek-v4-flash
  provider : deepseek

  Network/transport failure — DNS, TLS verification, a dead proxy or a
  firewall. The SDK reports all of these identically.

  Next steps:
    env | grep -iE 'proxy'    # a stale HTTPS_PROXY is the usual cause
    curl -sS -m 5 -o /dev/null -w '%{http_code}\n' https://api.deepseek.com/
    401 from curl = network works (so the key is the problem);
    no response at all = blocked by DNS/VPN/firewall.
    Or pass --api-base <url>, or --provider openai.

  No LLM needed (builds every analysis offline):
    DOCTOR=1 scripts/run-all-agentic.sh
```

Classification is by status code first, then by class name, so each failure mode
gets its own guidance — **401/403** key or scope, **404** wrong model id or
`--api-base`, **429** rate limit/quota, **400** context length or a rejected tool
schema, **5xx** provider-side. Our own bugs are deliberately *not* wrapped: only
genuine SDK/transport errors get the help block, and `AGENT_DEBUG=1` restores the
full traceback. Ctrl-C reports how to inspect the run instead of dumping a
traceback.

If the network is genuinely blocked, use the offline path —
`DOCTOR=1 ./scripts/run-all-agentic.sh` builds every analysis without an LLM.

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

A **flat party sheet** — where contacts/addresses are inline columns in the same
file — is detected by header and routed through the normal `import` command:
one **party** (Customer *or* Supplier) + linked Contact + Address per row, all
idempotent. No `--doctype` is required; `--defaults` still applies (e.g.
Group/Territory for customers).

| Sheet | Party column | Flow | Party doctype |
|---|---|---|---|
| Customers | `Customer Name`, `Customer Type`, `Group`, `Territory` | `customers_full` | Customer |
| Suppliers | `Supplier Name`, `Supplier Type`, `Supplier Group` | `suppliers_full` | Supplier |

Party types live in one registry (`PARTY_SPECS` in `erpgen/customers_full.py`),
so adding one is data, not code; everything else (contact/address columns,
dedup, link-merge, analysis) is shared. Party-specific quirks are declared
there too — e.g. Supplier (unlike Customer) has its own `country` field, so
`mirror_columns` feeds the sheet's Country column to the Address **and** the
Supplier record.

Source values are converted to each target field's type on the way in. This
matters: Frappe coerces `"Yes"`/`"true"` on a `Check` field to **0**, so an
uncoerced flat sheet would silently store `is_transporter = 0`.

**Contacts/Addresses are shared across parties.** A Contact dedups by email and
an Address by `address_title + address_type`; when one already exists — on the
site, earlier in the run, **or linked to a different party type** — it receives
an extra `Dynamic Link` row for this party instead of being re-created
(`linked` in the summary). So the same company can be both a Customer and a
Supplier and share one Contact and one Address:

```bash
python3 erpgen.py import samples/customers-smb.csv --apply   # creates Acme (Customer) + contact + address
python3 erpgen.py import samples/suppliers-smb.csv --apply   # links that SAME contact/address to Acme (Supplier)
```

Per-row insert failures (e.g. a required `address_line1`/`city`/`country`
missing) are logged as `failed` with a `WARNING:` on stderr and skipped — they
never abort the run. A failed record is **not** added to its dedup set, so
fixing the source and re-running retries it and re-links it. Applied runs log to
`logs/<flow>-<ts>.jsonl` (one `row` event per doctype), which is **not** yet
consumed by `revert` (that reads `logs/import-*.jsonl`).

### Out-of-contract columns & the flat contract override

Columns **not** in a flow's fixed contract (e.g. `Tax ID`, `Website`,
`Loyalty Tier`, `Customer Since`) are surfaced by `map`/`import` as
error-severity `unmapped_column` conflicts in that flow's analysis
(`customers_full` / `suppliers_full`), each with a `suggested_action`: map it to
an existing field, or create a custom field then map it. Resolve them by
extending the flat contract with `set-mapping`:

```bash
# maps to an existing field
python3 erpgen.py set-mapping customers_full --column "Tax ID" --target customer.tax_id
python3 erpgen.py set-mapping suppliers_full --column "Tax ID" --target supplier.tax_id

# needs a new field first
python3 erpgen.py createfield Customer --label "Customer Since" --fieldtype Date
python3 erpgen.py set-mapping customers_full --column "Customer Since" --target customer.customer_since
```

Flat targets are `<party>|contact|address.<fieldname>` (`customer`/`supplier`
for the party slot) and are validated against live metadata. Once a column is
mapped, it leaves the conflict list and its values import into the right doctype
(rerun `map` to confirm zero conflicts).

### `link_value_conflict` (mapped, but the values don't exist)

Mapping a column is only half the job: if the target field is a **Link**, its
values must exist as records. Choosing `Payment Terms` → `supplier.payment_terms`
is not enough — `Net 30` has to be a `Payment Terms Template`:

```json
{
  "kind": "link_value_conflict", "severity": "error",
  "source": "Payment Terms", "target": "supplier.payment_terms",
  "doctype": "Payment Terms Template",
  "missing_values": ["Letter of Credit", "Net 30", "Net 45"]
}
```

Every mapped Link column is checked — the fixed contract (`Group` →
`supplier_group`, `Country` → `country`) as well as resolved columns and
mirrors; a column mapped to two targets on the same linked doctype reports once
(`targets: ["address.country", "supplier.country"]`). The agent resolves it with
`create_record` and re-runs `map`. `describe-doctype` lists each child table's
**required fields and Select options** so the agent can build a valid row, e.g. a
Payment Terms Template needs `terms: [{invoice_portion, due_date_based_on}]`.

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
  journal.py   MigrationJournal — effects + the inverse that undoes each
  context.py   MigrationContext — one run file: requirements, sequenced effects
  overrides.py mapping overrides file (source of truth for decisions)
  tools.py     agent-facing primitives (records, fields, metadata)
erpgen.py     CLI: map | import | createfield | describe-doctype | get-record |
              list-records | set-mapping | create-record | revert | status | delete
scripts/agent.py  LlamaIndex AgentWorkflow agent (+ --doctor, --run)
samples/       demo CSV + XLSX
logs/          per-run audit logs (JSONL)
```

One migration = one `--run <id>` context (`logs/run-<id>.jsonl`) accumulating
requirements and sequenced effects across commands; `status` reads it, `revert`
undoes it. Without `--run`, each command writes its own `journal-*.jsonl`.

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

# flat party sheets (party + Contact + Address): full agentic loop — the LLM
# resolves out-of-contract columns (create fields + set_mapping) then imports
.venv/bin/python scripts/agent.py --source samples/customers-smb.csv
.venv/bin/python scripts/agent.py --source samples/suppliers-smb.csv
```

`--doctype` is optional; the agent infers it from the source headers like the
CLI does (flat party sheets are handled as doctype `customers_full` /
`suppliers_full`). Provider:
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

## Revert (journal-based undo — preferred)

Every mutation is journaled **at effect time** with the *inverse* that undoes it
(`logs/journal-<doctype>-<ts>.jsonl`). `revert` replays those inverses
newest-first, so **one command** undoes a whole run — records, custom fields and
mapping overrides together:

```bash
python3 erpgen.py revert --latest Item          # dry run: list the inverses
python3 erpgen.py revert --latest Item --apply  # execute them
python3 erpgen.py revert logs/journal-item-<ts>.jsonl --apply
```

Journaled effect → inverse:

| Effect | Inverse |
|---|---|
| record created (import / agent `create_record`) | delete that record |
| custom field created (`createfield` / agent `create_field`) | drop the Custom Field (drops the column + data) |
| mapping override set/unset (`set-mapping`) | restore the previous override value |

This is the **authoritative** undo path: it replays recorded inverses rather than
re-deriving intent from logs, and it covers mixed effects in one shot. Agent runs
journal automatically (their in-process `create_field`/`create_record`/
`set_mapping` calls are recorded against the run's journal).

Revert is **safe to repeat**: a successfully reverted journal gets an appended
`revert` marker, so reverting it again reports `Already reverted` instead of
re-deleting things (use `--force` to replay). Deleting something that is already
gone counts as success, not failure.

`--latest <DOCTYPE>` picks the newest log for that doctype **by modification
time** (run ids are arbitrary, so they do not sort chronologically), looking at
both `run-*.jsonl` and `journal-*.jsonl`, and skipping journals that were already
reverted.

## Unified migration context (`--run`)

Pass the same `--run <id>` to every command and they share **one** context file,
`logs/run-<id>.jsonl`, spanning the whole migration — mapping, field/record
creation and the import itself:

```bash
RUN=acme-01
python3 erpgen.py --run $RUN map samples/items_e2e.csv --doctype Item   # raises requirements
python3 erpgen.py --run $RUN createfield Item --label "Notes"           # satisfies one
python3 erpgen.py --run $RUN create-record UOM --fields '{"uom_name": "Dozen"}'
python3 erpgen.py --run $RUN set-mapping Item --column Group --target item_group
python3 erpgen.py status $RUN                                           # requirements + effects
python3 erpgen.py --run $RUN import samples/items_e2e.csv --doctype Item --apply
python3 erpgen.py revert $RUN --apply                                   # undo the whole run
```

Two things make this more than bookkeeping:

- **Requirements (coeffects).** `map` records every conflict as a *requirement*
  on the run. Requirements are re-checked against **every** effect from **any**
  later command, so a fix closes the requirement it addresses and `status` shows
  `0 pending` — the import gate then opens on its own. Multi-value requirements
  stay open until *all* values are fixed (creating UOM `Dozen` alone does not
  close "`Dozen` and `Roll` are missing"), and satisfying an `ambiguous_mapping`
  requirement requires choosing a target (`set-mapping`), not just touching it.
- **Sequenced, cross-command effects.** Effect sequence numbers continue across
  processes (`#1 … #11`), and because the run file is re-read on open, an effect
  applied in one command and a requirement raised in another still meet.
- **Requirements are identified, not counted twice.** `map` and `import` both
  re-run the mapper, so the same conflict is seen repeatedly; a requirement is
  keyed by `(kind, source, target, doctype, field)` and only recorded once per
  run while it stays open. If a requirement that was satisfied is raised again
  (its fix was undone), it reappears flagged `(REOPENED)` rather than silently
  staying "satisfied".

Effects → requirement they satisfy:

| Effect | Closes |
|---|---|
| `custom_field_create` (label or fieldname matches) | `unmapped_column` |
| `record_create` (all missing values present) | `link_value_conflict` |
| `override_set` (keyed on the ambiguous source column) | `ambiguous_mapping` |

If the artifact a fix needs **already exists**, the requirement is satisfied as a
*condition* with no effect journaled — so a later `revert` never deletes data this
run did not create:

```
[warning] ambiguous_mapping   satisfied (effect)     'Group' scores equally for item_group, ...
[info   ] unmapped_column     satisfied (condition)  Source column has no matching ERPNext field
```

The agent joins the same context with `--run`: the mapper's conflicts seed the
run's requirements and the agent's `create_field` / `create_record` /
`set_mapping` tool calls close them, so an agentic migration is revertible with
one command and leaves a single auditable log.

### Testing the agent without an API key

`scripts/mock-llm.py` is a tiny OpenAI-compatible stand-in (streaming included,
since `FunctionAgent` streams) — handy for verifying a change to the agent end to
end without spending credits or needing egress:

```bash
python3 scripts/mock-llm.py 8765 &
DEEPSEEK_API_KEY=dummy .venv/bin/python scripts/agent.py --run mock-01 \
    --source samples/customers_e2e.csv --doctype Customer \
    --provider deepseek --api-base http://127.0.0.1:8765/v1 --max-rounds 1
```

It answers the first turn with a `describe_doctype` tool call and the next with a
final text message. Note the agent needs the venv python (`.venv/bin/python`),
not the system `python3` — `llama_index` lives there.

## Tests

```bash
.venv/bin/python -m pytest              # whole unit suite (no stack needed)
.venv/bin/python -m pytest tests/test_journal.py -v
```

The unit suite is **pure**: no network, no live ERPNext, no Docker. Everything is
built in-process (`DoctypeMeta.from_api`, `SourceTable`, a `FakeClient`) or written
to pytest's `tmp_path`. It covers the P0 state machines — the layers where every
logic bug in this project has actually lived:

| file | covers |
|---|---|
| `tests/test_context_requirements.py` | requirement identity/ids across commands, partial coverage, condition-vs-effect satisfaction, regression reopening |
| `tests/test_context_effects.py` | effect sequencing across processes, replay on reopen, close idempotence, corrupt-log tolerance, read views |
| `tests/test_journal.py` | journal parsing, inverse application (incl. already-gone), LIFO replay, idempotent re-revert, revert markers |
| `tests/test_overrides.py` | overrides load/save/set/unset, forced mappings, required-field recomputation |
| `tests/test_run_selection.py` | `resolve_run`/`latest_run` — newest-by-mtime, skipping reverted, path-traversal safety |
| `tests/test_cli_args.py` | every subcommand parses and carries the global flags, `_snake`, `_json_arg`, `_inject_id_column` |

Regression tests are written against bugs that actually shipped, so the suite is
verified the other way round too:

```bash
.venv/bin/python scripts/mutation-check.py
```

That script rewrites each fixed bug back to its buggy form, runs the targeted
test, and expects a failure ("all 15 mutations caught"). Add a mutation whenever
you add a regression test — a test that passes against the bug is worthless.

Integration coverage remains the shell scripts (`scripts/test-items-import.sh`,
`scripts/verify-demo.sh`), which need the demo stack up.

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
