#!/usr/bin/env bash
# Run the full agentic erpgen flow over every master worksheet we support.
#
#   export DEEPSEEK_API_KEY=...
#   ./scripts/run-all-agentic.sh              # LLM resolves conflicts, then imports
#   DOCTOR=1 ./scripts/run-all-agentic.sh     # no LLM: build each map + show conflicts
#   RUN_ID=myrun ./scripts/run-all-agentic.sh # join a named run (revertable together)
#
# Sheets run in dependency order: parties first, then the Contact/Address sheets
# that link to them, then items. Overlaps are safe — imports are idempotent.
#
# Must run with the project venv (llama-index for the agent, openpyxl for .xlsx).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
PROVIDER="${PROVIDER:-deepseek}"
MAX_ROUNDS="${MAX_ROUNDS:-20}"
RUN_ID="${RUN_ID:-master-$(date +%Y%m%d-%H%M%S)}"
DOCTOR="${DOCTOR:-}"

# master worksheets we support, in dependency order
SHEETS=(
  customers-smb.csv    # flat customers_full: Customer + Contact + Address
  customers.csv        # Customer (relational)
  customers_e2e.csv    # Customer (conflict-rich: unmapped columns)
  customers.xlsx       # Customer (xlsx reader path)
  suppliers-smb.csv    # flat suppliers_full: Supplier + Contact + Address
  contacts.csv         # Contact — links to the customers above
  addresses.csv        # Address — links to the customers above
  items.csv            # Item
  items_e2e.csv        # Item (conflict-rich)
)

if [[ -z "$DOCTOR" && -z "${DEEPSEEK_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: no LLM key set. export DEEPSEEK_API_KEY=... (or OPENAI_API_KEY)" >&2
  echo "       For a no-LLM dry pass: DOCTOR=1 $0" >&2
  exit 2
fi

echo "run id : $RUN_ID"
echo "mode   : ${DOCTOR:+no-LLM (map only)}${DOCTOR:-agentic (LLM resolves + imports)}"
echo "python : $PY"

for sheet in "${SHEETS[@]}"; do
  echo
  echo "======================================================================"
  echo "== $sheet"
  echo "======================================================================"
  if [[ -n "$DOCTOR" ]]; then
    if ! "$PY" erpgen.py map "samples/$sheet"; then
      echo "!! map failed for $sheet" >&2
      exit 1
    fi
  else
    if ! "$PY" scripts/agent.py --source "samples/$sheet" \
         --provider "$PROVIDER" --max-rounds "$MAX_ROUNDS" --run "$RUN_ID"; then
      echo "!! agent failed for $sheet" >&2
      exit 1
    fi
  fi
done

echo
echo "======================================================================"
echo "DONE — $RUN_ID"
echo "======================================================================"
if [[ -z "$DOCTOR" ]]; then
  # status/revert take the run id positionally; revert previews unless --apply
  "$PY" erpgen.py status "$RUN_ID" || true
  echo
  echo "inspect requirements/effects : $PY erpgen.py status $RUN_ID"
  echo "undo this whole run          : $PY erpgen.py revert $RUN_ID --apply"
fi
