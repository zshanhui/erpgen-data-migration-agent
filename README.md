# README.md

Deterministic and verifiable data migration agent for ERPNext

Full demo test run

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

Running full agentic (LLM) workflow

```txt

```
