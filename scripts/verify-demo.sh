#!/usr/bin/env bash
# Verify the local ERPNext demo is up and probe the REST API surface
# that the migration tool will use. Run from repo root.
# Requires: site up AND setup wizard run (see scripts/setup-demo.sh).
set -euo pipefail

BASE="${BASE:-http://localhost:8082}"
CURL=(curl -s -m 15)

echo "== 1. site reachable =="
"${CURL[@]}" -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/api/method/ping" || true

echo "== 2. login (Administrator/admin) =="
LOGIN=$("${CURL[@]}" -c /tmp/erpnext_cookies.txt -d 'usr=Administrator&pwd=admin' "$BASE/api/method/login")
echo "$LOGIN" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("login ok, home:", d["home_page"])'

echo "== 3. DocType metadata for Customer (field count) =="
META=$("${CURL[@]}" -b /tmp/erpnext_cookies.txt "$BASE/api/resource/DocType/Customer")
echo "$META" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("fields:", len(d["data"]["fields"]), "| istable:", d["data"]["istable"])'

echo "== 4. master data counts =="
for dt in "Customer Group" "Territory" "Item Group" "UOM" "Company" "Warehouse"; do
  enc=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$dt")
  n=$("${CURL[@]}" -b /tmp/erpnext_cookies.txt "$BASE/api/resource/$enc?limit_page_length=0" \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo "?")
  echo "  $dt: $n"
done

echo "== 5. insert + delete a test Customer =="
INSERT=$("${CURL[@]}" -b /tmp/erpnext_cookies.txt -H 'Content-Type: application/json' \
  -d '{"customer_name":"Migration Test Co","customer_type":"Company","customer_group":"Commercial","territory":"All Territories"}' \
  "$BASE/api/resource/Customer")
NAME=$(echo "$INSERT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["name"])')
echo "  created: $NAME"
DEL=$("${CURL[@]}" -b /tmp/erpnext_cookies.txt -X DELETE "$BASE/api/resource/Customer/$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$NAME")")
echo "  deleted: $(echo "$DEL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"])')"

rm -f /tmp/erpnext_cookies.txt
echo "DONE - demo healthy"
