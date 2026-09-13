# README.md

Deterministic and verifiable data migration agent for ERPNext

full demo test run

make sure you have erpnext stack running in Docker before runningg erpgen

```
export DEEPSEEK_API_KEY=...
cd data-migration

./scripts/run-all-agentic.sh                # LLM resolves conflicts, then imports
DOCTOR=1 ./scripts/run-all-agentic.sh       # no LLM: build each map + show conflicts
RUN_ID=myrun ./scripts/run-all-agentic.sh   # name the run so it reverts as one unit
```

```
# customers: 29 rows (20 will be new, nothing pre-exists)
python3 erpgen.py import samples/customers.csv --doctype Customer \
    --defaults '{"customer_group":"Commercial","territory":"All Territories"}' --apply

# items: 12 rows, UoM→uoms.uom trap + Machinery-group conflict back
python3 erpgen.py map samples/items.csv --doctype Item       # see conflicts first

# or the full agent loop on the conflict-rich e2e file
export DEEPSEEK_API_KEY=<your-key>
.venv/bin/python scripts/agent.py --doctype Item \
    --source samples/items_e2e.csv --provider deepseek
```

Running deterministic mappings:

```txt
data-migration % python3 erpgen.py import samples/customers_e2e.csv --doctype Customer
Applied 1 mapping override(s) from mapping-overrides.json

Mapping plan for Customer  (6 source rows)
SOURCE COLUMN               TARGET FIELD                    SCORE  METHOD      NOTES
Customer Name               customer_name                   1.00   exact       
Customer Type               customer_type                   1.00   exact       
Group                       customer_group                  1.00   override    Link field; values must exist in Customer Group
Territory                   territory                       1.00   exact       Link field; values must exist in Territory
Vendor Code                 vendor_code                     1.00   exact       
Notes                       notes                           1.00   exact       
Internal Ref                (unmapped)                      0.00   none        

Warnings:
  ! Source column 'Internal Ref' has no matching ERPNext field; its values will be dropped
  ! Read-only fetch field 'mobile_no' left alone (good): populated from customer_primary_contact.mobile_no
  ! Read-only fetch field 'email_id' left alone (good): populated from customer_primary_contact.email_id
  ! Read-only fetch field 'first_name' left alone (good): populated from customer_primary_contact.first_name
  ! Read-only fetch field 'last_name' left alone (good): populated from customer_primary_contact.last_name

Link fields (values validated against the target site):
  -> customer_group (Link -> Customer Group)
  -> territory (Link -> Territory)

Prepared 6 payloads for Customer
  id_field: 'name' | key field: 'customer_name' | id_column: 'Customer Name'
Analysis saved to analysis/analysis-customer-20260908-101644090896.json  (3 conflicts, 1 suggested custom fields)
  of 6 keyed rows: 6 new, 0 already exist (will be skipped)

Dry run (use --apply to import). 
First payload (REST upsert path):
{
  "customer_name": "Nimbus Forge Co",
  "customer_type": "Company",
  "customer_group": "Wholesale",
  "territory": "All Territories",
  "vendor_code": "VC-9001",
  "notes": "agent e2e test customer"
}
```

full agentic migrations run for single customers worksheet

```
MacBook-Pro data-migration % .venv/bin/python scripts/agent.py --source samples/customers.csv --run contacts-run
LLM preflight: https://api.deepseek.com reachable (HTTP 404 at the root — normal for many API hosts)
Starting agent for Customer — 2 conflicts
  [warning] fetch_from             Email
  [warning] fetch_from             Phone
Run context: /erpgen/data-migration/logs/run-contacts-run.jsonl  (0 requirement(s) recorded)

=== Round 1/20 — 0 error conflict(s), 2 total ===
  · llm #1 → 2 msg(s)
  · llm #1 ← 2216ms · 3,971 tok
      “I'll start by re-running the mapper to get the freshest analysis and confirm the error-severity conflict count.”
    ⚙ run_map(source=samples/customers.csv, doctype=Customer)
      ✓ map exit 0. Fresh analysis:
  · llm #2 → 4 msg(s)
  · llm #2 ← 2386ms · 6,369 tok
      “The fresh analysis shows zero **error-severity** conflicts. The only two conflicts are `fetch_from` **warnings** (Email→email_id, Phone→mobile_no) — these are read-only …”
    ⚙ run_import(source=samples/customers.csv, doctype=Customer, apply=True)
      ✓ import exit 0:
  · llm #3 → 6 msg(s)
  · llm #3 ← 1552ms · 7,076 tok
      “The import completed successfully. All 29 rows were already present (idempotent skip). Let me verify a couple of records.”
    ⚙ list_records(doctype=Customer, filters_json={"customer_name": ["in", ["Acme Steel Works"…)
    ⚙ get_record(doctype=Customer, name=Acme Steel Works)
      ✓ [
      ✓ {
  · llm #4 → 9 msg(s)
  · llm #4 ← 3189ms · 8,205 tok
      “The migration for **Customer** is complete. Here's the summary: ## Conflict resolution **Error-severity conflicts: 0** — nothing was blocking the import. I confirmed thi…”
--- Round 1: 0 error(s) remain

=== AGENT CONVERGED (no error-severity conflicts) ===

The migration for **Customer** is complete. Here's the summary:

## Conflict resolution

**Error-severity conflicts: 0** — nothing was blocking the import. I confirmed this by re-running the mapper; it returned the same clean state as the original analysis.

**Remaining conflicts — 2 × `fetch_from` (severity: warning, non-blocking):**
| Source | Read-only target | Populated from | Action |
|--------|-----------------|----------------|--------|
| Email | `email_id` | `customer_primary_contact.email_id` | Skipped — must be written on the linked Contact |
| Phone | `mobile_no` | `customer_primary_contact.mobile_no` | Skipped — must be written on the linked Contact |

These are ERPNext read-only fetch fields. Direct writes are silently discarded, so they were dropped from the payloads (the mapper logs this). No `set_mapping`/`create_field`/`create_record` action is possible or needed for them — they populate automatically once the primary Contact's `email_id`/`mobile_no` are set.

## Import

- `run_import(apply=True)` → exit 0
- **29/29 rows already existed → skipped** (idempotent upsert), 0 created, 0 failed
- Link fields `customer_group` (→ Customer Group) and `territory` (→ Territory) validated successfully against the site — no `link_value_conflict`, so no option records needed.

## Verification

- `list_records` confirmed Acme Steel Works, Bluedot Logistics, Maple Grove Organics present.
- `get_record('Customer', 'Acme Steel Works')` confirms mappings landed correctly: `customer_type=Company`, `customer_group=Commercial`, `territory=All Territories`, `tax_id=US-88421`, `notes` set, and the child table `credit_limits` populated with `credit_limit=15000.0`.

## Note for follow-up (out of scope, no data loss)
`email_id`, `mobile_no`, `first_name`, `last_name` on these Customers are empty. To populate them, the values must be written to each Customer's **primary Contact** record (`customer_primary_contact`), not to the Customer directly. The source Email/Phone columns were intentionally not written.

No source files were modified; all decisions are recorded via the mapper's mapping plan.

Run context: /erpgen/data-migration/logs/run-contacts-run.jsonl  (0 effect(s), 2 requirement(s) still pending)
    PENDING  fetch_from: Read-only field; populated from customer_primary_contact.email_id. Direct writes are discarded.
    PENDING  fetch_from: Read-only field; populated from customer_primary_contact.mobile_no. Direct writes are discarded.
  revert the whole run: python3 erpgen.py revert contacts-run --apply

Agent transcript (LLM calls + tool calls + responses): /erpgen/data-migration/logs/agent-customer-20260913-074409402790.jsonl
  4 remote LLM call(s) logged — grep 'llm_request' / 'llm_failure'
```
