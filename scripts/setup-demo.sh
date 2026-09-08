#!/usr/bin/env bash
# First-run setup for a fresh ERPNext site: runs the setup wizard via API.
# A newly created site has NO company / master data until this runs.
# Usage: bash scripts/setup-demo.sh   (idempotent: wizard no-ops if setup done)
set -euo pipefail

BASE="${BASE:-http://localhost:8082}"
CURL=(curl -s -m 300)

"${CURL[@]}" -c /tmp/erpnext_cookies.txt -d 'usr=Administrator&pwd=admin' "$BASE/api/method/login" >/dev/null

"${CURL[@]}" -b /tmp/erpnext_cookies.txt -H 'Content-Type: application/json' \
  "$BASE/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete" \
  -d '{"args":{
        "company_name":"Demo Manufacturing",
        "company_abbr":"DM",
        "company_tagline":"Local ERPNext demo",
        "currency":"USD",
        "country":"United States",
        "timezone":"America/New_York",
        "language":"english",
        "chart_of_accounts":"Standard",
        "bank_account":"Cash",
        "fy_start_date":"2026-01-01",
        "fy_end_date":"2026-12-31",
        "domain":"Manufacturing",
        "enable_telemetry":0
      }}' | head -c 300; echo
rm -f /tmp/erpnext_cookies.txt
echo "Setup wizard complete. Masters created: Customer Groups, Territories, Item Groups, UOMs, Company, Cost Centers, Warehouses, Price Lists."
