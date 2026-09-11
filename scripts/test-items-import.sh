#!/usr/bin/env bash
# Reset-and-import test for the items sample.
#
# Runs the full migration workflow against the live ERPNext demo:
#   1. reset  — delete the demo items (idempotent cleanup, missing = ok)
#   2. import — map + idempotent import with --apply
#   3. verify — assert every expected item exists on the site
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
# b) 'Machinery' item group is a lookup record the sample references
"$PY" - <<'PYEOF'
import os, sys; sys.path.insert(0, ".")
from erpgen.client import ERPNextClient
from erpgen.tools import create_record
c = ERPNextClient(os.environ.get("BASE", "http://localhost:8082"))
r = create_record(c, "Item Group", {"item_group_name": "Machinery",
                                    "parent_item_group": "All Item Groups"})
print(f"  item group Machinery: {r}")
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
echo "PASS — items import workflow OK"
