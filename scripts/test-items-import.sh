#!/usr/bin/env bash
# Reset-and-import test for the items sample.
#
# Runs the full migration workflow against the live ERPNext demo:
#   1. reset      — delete the demo items (idempotent cleanup, missing = ok)
#   2. prereqs    — the Item Groups the sheet needs (via create_record)
#   3. import     — map + idempotent import with --apply
#   4. verify     — assert every expected item exists on the site
#   5. idempotency— re-import: must create 0 and skip every row
#
# Pass/fail via exit code: 0 = pass, 1 = fail.
#
# Usage:  bash scripts/test-items-import.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
DOCTYPE="Item"
SOURCE="samples/items.csv"
ITEM_CODES=(MFG-1001 RAW-2001 RAW-2002 SUB-3001 FIN-4001 SVC-5001 \
            MFG-1002 PKG-6001 FIN-4002 RAW-2003 BOGUS-7001 BOGUS-7002)

echo "=================================================================="
echo " items import test — $(date '+%Y-%m-%d %H:%M:%S')"
echo " doctype=$DOCTYPE source=$SOURCE"
echo "=================================================================="

echo
echo "== 1. reset: delete demo items (missing ones are fine) =="
names="$(IFS=,; echo "${ITEM_CODES[*]}")"
"$PY" erpgen.py delete --doctype "$DOCTYPE" --names "$names" 2>&1 || true

echo
echo "== 2. resolve prerequisites (what the agent would do) =="
# a) the UoM column scores against the child table 'uoms.uom' — force the parent field
"$PY" erpgen.py set-mapping "$DOCTYPE" --column "UoM" --target stock_uom 2>&1 | grep -E "Override saved|Journal" || true
# b) every Item Group the sheet references is a lookup record it must exist first;
#    creating them through `create_record` is also the first exercise of its
#    "does this already exist?" query (the thing the second pass below gates on)
"$PY" - <<'PYEOF'
import csv, os, sys; sys.path.insert(0, ".")
from erpgen.client import ERPNextClient
from erpgen.tools import create_record

client = ERPNextClient(os.environ.get("BASE", "http://localhost:8082"))
needed = sorted({r["Group"].strip() for r in csv.DictReader(open("samples/items.csv"))
                 if r.get("Group", "").strip()})
have = {r["name"] for r in client.list("Item Group", fields=["name"], limit=0)}
missing = [g for g in needed if g not in have]
for group in missing:
    result = create_record(client, "Item Group",
                           {"item_group_name": group,
                            "parent_item_group": "All Item Groups"})
    print(f"  item group {group}: {result}")
print(f"  item groups: {len(needed)} needed, {len(missing)} created")
PYEOF

echo
echo "== 3. import (--apply) =="
OUT="$("$PY" erpgen.py import "$SOURCE" --doctype "$DOCTYPE" --apply 2>&1)"
echo "$OUT" | sed -n '/Dedup:/,$p'

if echo "$OUT" | grep -qE "failed: [1-9]|failed [1-9]|error-severity conflict"; then
    echo "FAIL: import reported failures or was blocked by conflicts" >&2
    exit 1
fi
CREATED="$(echo "$OUT" | sed -n 's/.*created \([0-9]*\).*/\1/p' | head -1)"
echo "  (import created $CREATED records)"

echo
echo "== 4. verify: all 12 items present with expected key fields =="
MISSING=0
for code in "${ITEM_CODES[@]}"; do
    doc="$("$PY" erpgen.py get-record "$DOCTYPE" "$code" 2>/dev/null)" || { MISSING=1; echo "  MISSING: $code"; continue; }
    echo "$doc" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ok = all(d.get(k) for k in ('item_group', 'stock_uom'))
print(f\"  OK  {d['item_code']:<12} group={d.get('item_group',''):<14} uom={d.get('stock_uom',''):<4} stock={d.get('is_stock_item')}\")
sys.exit(0 if ok else 1)
" || { MISSING=1; echo "  BAD-FIELDS: $code"; }
done

if [ "$MISSING" -ne 0 ]; then
    echo "FAIL: one or more items missing or malformed" >&2
    exit 1
fi

echo
echo "== 5. idempotency: re-importing the same sheet must create nothing =="
# This is the only check of the real boundary's key lookup: every record now
# exists, so a filter that cannot match (wrong field, wrong value) shows up here
# as duplicates instead of a no-op. The unit suite fakes the client, so nothing
# else catches it.
OUT2="$("$PY" erpgen.py import "$SOURCE" --doctype "$DOCTYPE" --apply 2>&1)"
echo "$OUT2" | sed -n '/Dedup:/,$p'

if echo "$OUT2" | grep -qE "failed: [1-9]|failed [1-9]|error-severity conflict"; then
    echo "FAIL: the re-import reported failures or was blocked by conflicts" >&2
    exit 1
fi
CREATED2="$(echo "$OUT2" | sed -n 's/.*created \([0-9]*\).*/\1/p' | head -1)"
SKIPPED2="$(echo "$OUT2" | sed -n 's/.*to create, \([0-9]*\) skipped.*/\1/p' | head -1)"
if [ "${CREATED2:-x}" != "0" ] || [ "${SKIPPED2:-x}" != "${#ITEM_CODES[@]}" ]; then
    echo "FAIL: re-import created ${CREATED2:-?}, skipped ${SKIPPED2:-?} — expected 0 created, ${#ITEM_CODES[@]} skipped." >&2
    echo "      The existence query is not matching what is on the site." >&2
    exit 1
fi
echo "  (re-import created 0, skipped $SKIPPED2)"

echo
echo "PASS — items import workflow OK (including idempotent re-import)"
