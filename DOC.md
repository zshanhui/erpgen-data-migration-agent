# erpgen — agentic Excel/ERP → ERPNext migration tooling

Converts CSV/XLSX into ERPNext, using the **live site's DocType metadata** as the
ground truth for mapping. A deterministic CLI, plus an LLM agent that resolves
whatever the mapping cannot decide on its own.

## Agent runs

The agent reads the mapping analysis, resolves what scoring cannot decide, then
imports — calling the same CLI underneath. Point it at one sheet:

```bash
export DEEPSEEK_API_KEY=...
.venv/bin/python erpgen.py --run myrun agent --source samples/suppliers-smb.csv --provider deepseek
.venv/bin/python erpgen.py --run myrun agent --source samples/items.csv --provider deepseek   # any other sheet
```

Global flags (`--base`, `--log-dir`, `--run`) live on the CLI itself, so they come
**before** the subcommand.

The doctype is **inferred per sheet**. Check the `Starting agent for <doctype>`
line matches the sheet (`items.csv` → `Item`); a mismatch means every column is
being analysed against the wrong doctype.

### Every master worksheet

`scripts/run-all-agentic.sh` drives the LLM loop over all supported sheets in
dependency order (parties → the Contact/Address sheets that link to them → items).

```bash
export DEEPSEEK_API_KEY=...
./scripts/run-all-agentic.sh               # LLM resolves conflicts, then imports
DOCTOR=1 ./scripts/run-all-agentic.sh      # no LLM: build each map, show conflicts
RUN_ID=myrun ./scripts/run-all-agentic.sh  # name the run so it reverts as one unit
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

### Deterministic first, LLM only when needed

With **no error-severity conflicts** there is nothing for the model to decide, so
the agent imports directly and only involves the LLM if rows actually failed:

```
Starting agent for Item — 0 conflicts

No conflicts — deterministic import (exit 0): 0 row(s) failed
```

A clean re-run therefore costs **zero** remote calls (and needs no API key). When
rows do fail, the failure digest is handed to the agent so it starts from the
evidence instead of rediscovering it. `--always-llm` restores the old behaviour.

### Following a run

The agent streams each LLM call and tool invocation, so a slow round is not a
blank screen. Tool calls are grep-able:

```bash
python3 erpgen.py agent --source samples/items.csv | grep ToolUse:
python3 erpgen.py agent --source samples/items.csv | tee run.log | grep 'ToolResult:.*✗'
```

`--quiet` suppresses the per-call stream (round headers still print). Everything
also lands in `logs/agent-<doctype>-<ts>.jsonl` — one JSON object per line
covering LLM calls (duration, token usage — never prompt content), tool calls and
rounds:

```bash
grep -c llm_request logs/agent-suppliers_full-*.jsonl   # how many remote calls
grep llm_failure  logs/agent-suppliers_full-*.jsonl     # any failed calls
```

### Testing without a key

`scripts/mock-llm.py` is a small OpenAI-compatible stand-in (streaming included):

```bash
python3 scripts/mock-llm.py 8765 &
DEEPSEEK_API_KEY=dummy .venv/bin/python erpgen.py --run mock-01 agent \
    --source samples/customers_e2e.csv --doctype Customer \
    --provider deepseek --api-base http://127.0.0.1:8765/v1 --max-rounds 1
```

## Quick start

Connection defaults: `--base http://localhost:8082 --user Administrator --password admin`.

```bash
python3 erpgen.py map samples/customers.csv               # plan + analysis only
python3 erpgen.py import samples/customers.csv            # dry run: payloads + predicted dedup
python3 erpgen.py import samples/customers.csv --apply    # idempotent import
```

- **Idempotent.** Only new records are created; existing ones are skipped, so
  re-running the same source is a safe no-op.
- **Gated.** `--apply` refuses to run while error-severity conflicts remain
  (exit 2). Resolve them, or pass `--bypass-conflicts` to import anyway.
- **`--doctype` is optional** — inferred from the headers (see
  [Doctype inference](#doctype-inference)).
- Use `.venv/bin/python` for `.xlsx` sources and for the agent.

## How mapping works

1. Every source column is **scored** against the live doctype (exact, normalized,
   token, synonym, fuzzy).
2. `mapping-overrides.json` is applied **on top** — recorded decisions beat
   scoring.
3. An **analysis** is written to `analysis/analysis-<doctype>-<ts>.json`
   describing what is still unresolved, for the agent (or you) to act on. The
   newest 10 per doctype are kept.

What the analysis can report:

| kind | severity | meaning |
|---|---|---|
| `unmapped_column` | info / error | column has no home — map it, or create a field |
| `ambiguous_mapping` | warning | two fields score equally — choose with `set-mapping` |
| `required_missing` | error | required field with no source and no default |
| `link_value_conflict` | error | a Link value has no matching record on the site |
| `link_group_node` | error | the value is a Group node, but the field needs a leaf |
| `fetch_from` | warning | read-only field — write the source doc instead |

## Doctype inference

Used when `--doctype` is omitted, in priority order:

1. **Header identity columns** — `Customer Name`, `Supplier Name`, `Item Code`,
   `First Name`/`Contact Name`, `Address Title`/`Address Line 1`,
   `Customer Group Name`/`Item Group Name`/`Supplier Group Name`.
2. **File-name prefix** — `customers*.csv`, `items*.csv`, `addresses*.csv`,
   `contacts*.csv`, `customer_groups*.csv` (case-insensitive).

If neither matches, the tool errors (exit 2) rather than guess.

## Tree sheets

A sheet importing a tree doctype (Customer Group, Item Group, Supplier Group)
**names its own parents**: `parent_customer_group` holds Customer Group names the
same file creates. Three things follow, all handled without editing the source:

```bash
python3 erpgen.py import samples/customer_groups.csv --apply   # doctype inferred
```

- **Those parents are not "missing".** Link validation would otherwise report
  `link_value_conflict` for values the sheet itself creates, and `--apply` refuses
  on error-severity conflicts — so a hierarchical sheet could never be applied.
  A parent found in neither the sheet nor the site is still reported.
- **Rows are imported parents-first.** ERPNext rejects a child whose parent does
  not exist yet (`LinkValidationError`), so a child listed above its parent in the
  source would fail. A cycle (including a row naming itself) can't be ordered:
  those rows keep their source order and a `NOTE:` names them.
- **`is_group` is derived** when the sheet has no Is Group column (or the column
  is blank): every row some *other* row names as its parent becomes a group.
  ERPNext does **not** reject a child under a leaf — it silently stores a leaf
  with children — so without this the tree is malformed rather than broken. An
  explicit Is Group value in the source always wins.

ERPNext maintains the nested set (`lft`/`rgt`) itself; the created records are
journaled and revert as one run like any other import.

**A party can only sit on a leaf.** `Customer.customer_group` is the one field in
ERPNext that validates this (`validate_customer_group`), throwing
`Cannot select a Group type Customer Group` — so a customer sheet whose Group
column names a parent group fails every such row at the site. That is reported up
front as `link_group_node` (error), not as a missing value: the record exists, and
the fix is to remap the value onto a leaf, not to create the group again. The check
is deliberately limited to `LEAF_ONLY_LINKS`; `Item.item_group` and
`Customer.territory` accept a group node, so flagging those would invent conflicts
ERPNext does not have.

## Flat party sheets

A sheet with contact/address columns inline (`customers-smb.csv`,
`suppliers-smb.csv`) is detected by header and imported as one **party**
(Customer or Supplier) + linked Contact + Address per row. No `--doctype` needed.

```bash
python3 erpgen.py import samples/customers-smb.csv --apply
python3 erpgen.py import samples/suppliers-smb.csv --apply
```

**Contacts and Addresses are shared across parties.** A Contact dedups by email,
an Address by `address_title + address_type`. When one already exists — on the
site, earlier in the run, or linked to a *different* party type — it gains an
extra `Dynamic Link` row for this party instead of being re-created (`linked` in
the summary). So one company can be both a Customer and a Supplier and share the
same Contact and Address.

**Columns outside the flat contract** (e.g. `Tax ID`, `Payment Terms`, `Website`)
are surfaced as conflicts with a suggested action. Resolve them by extending the
contract:

```bash
# onto an existing field
python3 erpgen.py set-mapping suppliers_full --column "Tax ID" --target supplier.tax_id

# or create the field first
python3 erpgen.py createfield Supplier --label "Lead Time Days" --fieldtype Int
python3 erpgen.py set-mapping suppliers_full --column "Lead Time Days" --target supplier.lead_time_days
```

Flat targets are `<customer|supplier|contact|address>.<fieldname>`, validated
against live metadata. Once mapped, the column leaves the conflict list.

**Per-row failures never abort a run.** A row missing required data (say
`address_line1`) is logged as `failed` with a `WARNING:` and skipped. It is not
added to its dedup set, so fixing the source and re-running retries it.

## Commands

| command | what it does |
|---|---|
| `map` | build the plan + analysis, touch nothing |
| `import` | map + idempotent import (dry run unless `--apply`) |
| `createfield` | create a custom field, giving an unmapped column a home |
| `describe-doctype` | required fields, Link targets, child tables (incl. child required fields) |
| `get-record` / `list-records` | read records as JSON |
| `set-mapping` | record a forced source-column → target-field decision |
| `create-record` | create a lookup record (Item Group, UOM, …) |
| `agent` | LLM loop over the analysis: resolve conflicts, then import (`--doctor` skips the LLM) |
| `status` | show a `--run` context: effects applied, requirements pending |
| `revert` | undo a journal / run by replaying its recorded inverses |
| `delete` | delete records by name (cleanup) |

```bash
python3 erpgen.py createfield Customer --label "Vendor Code" --fieldtype Data
python3 erpgen.py createfield Customer --label "Tier" --fieldtype Select \
    --options "Standard,Premium" --insert-after customer_group

python3 erpgen.py describe-doctype Item --all
python3 erpgen.py get-record Customer "Acme Steel Works"
python3 erpgen.py list-records "Customer Group" --filter '[["name","like","%Commercial%"]]'

python3 erpgen.py set-mapping Customer --column "Group" --target customer_group
python3 erpgen.py set-mapping Customer --list
python3 erpgen.py set-mapping Customer --unset "Group"
```

`describe-doctype` lists each child table's required fields and Select options,
which is what lets an agent build a valid row (a `Payment Terms Template` needs
`terms: [{invoice_portion, due_date_based_on}]`).

Flags worth knowing:

| flag | effect |
|---|---|
| `--bulk` / `--submit` | import via the Data Import machinery instead of REST, and submit docs |
| `--id-column` | which source column carries the natural key, when it can't be inferred |
| `--overrides` | use a different overrides file (e.g. per client) |
| `--analysis-dir` / `--log-dir` | where analyses and audit logs are written (defaults: `analysis/`, `logs/`) |

```bash
python3 erpgen.py import samples/sales_orders.csv --doctype "Sales Order" \
    --id-column "Sales Order ID" --apply
python3 erpgen.py import samples/customers.csv --apply --bulk --submit
```

## Mapping overrides

`mapping-overrides.json` is the **source of truth for decisions** — the source
spreadsheet is never edited. Keyed per doctype (or per flat flow):

```json
{ "Item": { "mappings": { "UoM": "stock_uom" } } }
```

Applied after scoring, so a recorded mapping always wins. Invalid targets are
rejected at write time; a stale entry is ignored with a warning rather than
failing a run. `--overrides <file>` selects a different file (e.g. per client).

## Undo

Every mutation is journaled **when it happens**, together with the inverse that
undoes it. `revert` replays those inverses newest-first, so one command undoes
records, custom fields and overrides together:

```bash
python3 erpgen.py revert --latest Item           # dry run: list the inverses
python3 erpgen.py revert --latest Item --apply   # execute them
```

Revert is **safe to repeat**: a reverted journal is marked, so a second run
reports `Already reverted` instead of re-deleting (`--force` replays). Deleting
something already gone counts as success. `--latest <DOCTYPE>` picks the newest
log for that doctype **by modification time**.

### One context per migration (`--run`)

Pass the same `--run <id>` to every command and they share one file,
`logs/run-<id>.jsonl`, spanning mapping, fixes and the import:

```bash
RUN=acme-01
python3 erpgen.py --run $RUN map samples/items_e2e.csv --doctype Item
python3 erpgen.py --run $RUN createfield Item --label "Notes"
python3 erpgen.py --run $RUN set-mapping Item --column Group --target item_group
python3 erpgen.py status $RUN                     # requirements + effects
python3 erpgen.py --run $RUN import samples/items_e2e.csv --doctype Item --apply
python3 erpgen.py revert $RUN --apply             # undo the whole run
```

`map` records each conflict as a **requirement**; later effects from *any*
command close the ones they address, so `status` shows `0 pending` and the import
gate opens on its own. A requirement stays open until *all* its values are fixed,
and a fix that is later undone reopens it. If the artifact a fix needs already
exists, the requirement is satisfied as a *condition* with no effect journaled —
so revert never deletes data this run did not create.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `exit 2`, "error-severity conflict(s) remain" | resolve them, or `--bypass-conflicts` |
| `exit 3`, "exhausted its internal step budget" | raise `--max-iterations` (default 50) |
| `exit 4`, "AGENT STALLED" | the same conflicts survived 2 rounds unchanged; tune `--max-stall-rounds` (0 disables) |
| `APIConnectionError` | network/proxy — see below |
| analysed against the wrong doctype | check `Starting agent for <doctype>` |
| `overrides file ... is not valid JSON` | the error names the line; fix it or delete the file |

**`APIConnectionError`.** The OpenAI SDK reports DNS failure, TLS errors, a dead
proxy and a refused connection identically. The agent probes the endpoint first
and prints the real cause:

```bash
env | grep -iE 'proxy'                                    # stale HTTPS_PROXY is the usual culprit
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' https://api.deepseek.com/
```

`401` means the network is fine and the key is the problem. Other statuses map to
their own guidance (404 → model id or `--api-base`, 429 → rate limit, 400 →
context length). No LLM needed at all:
`DOCTOR=1 ./scripts/run-all-agentic.sh`.

## Tests

```bash
.venv/bin/python -m pytest                 # unit suite — no stack, no network, no Docker
.venv/bin/python scripts/mutation-check.py # rewrite each fixed bug; the tests must fail
```

The suite is pure: everything is built in-process or written to `tmp_path`. The
mutation check is the real guarantee — it puts each fixed bug back and expects the
targeted test to fail, so a test that passes against the bug is worthless. Add a
mutation whenever you add a regression test.

Integration coverage is the shell scripts (`scripts/test-items-import.sh`,
`scripts/verify-demo.sh`), which need the demo stack up.

## Known limitations

- **Multi-row parents are not grouped** — one Sales Order spread over several
  source rows becomes several documents.
- **Flat party sheets are not covered by `revert`** — they log to
  `logs/<flow>-<ts>.jsonl`, which `revert` does not read.
- **A tree sheet cannot promote a group that already exists as a leaf.** Imports
  are create-or-skip, so a row that already exists keeps its `is_group`; the
  derived flag never reaches it, and ERPNext accepts children under a leaf. Check
  with `describe-doctype`/`get-record` and fix the existing row by hand before
  importing. (Real case: `Wholesale` and `Retail Chain` ship as leaves in the
  ERPNext demo, so a sheet that wants them as parents must not reuse those names.)
- **Flat party imports do not gate on conflicts.** The relational path refuses
  `--apply` while error-severity conflicts remain; flat party sheets import
  first and report afterwards, so a `link_group_node` or missing link value only
  shows up as per-row failures (the run still exits 0). `map` reports it either
  way — check the analysis before applying a flat sheet.

## Local demo stack

See `docker/README.md` (ERPNext v16 on :8082, OrbStack, setup wizard note).
