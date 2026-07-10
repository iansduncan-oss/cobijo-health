#!/usr/bin/env bash
# Zero-cost check on the moat extraction batch + a plain-English recommendation.
# Reads the batch id from data/extract_cache/pending_batch.json, queries Anthropic (GET only —
# no charge), and tells you exactly what to run next. Safe to run any number of times.
#
#   ./batch_status.sh
set -euo pipefail
cd "$(dirname "$0")"

PENDING=data/extract_cache/pending_batch.json
[ -f "$PENDING" ] || { echo "No $PENDING — no batch in flight. To (re)submit: python3 extract_llm.py --batch"; exit 0; }

KEY="${ANTHROPIC_API_KEY:-$(security find-generic-password -s claude-memory -a anthropic-api-key -w 2>/dev/null || true)}"
[ -n "$KEY" ] || { echo "No API key (env ANTHROPIC_API_KEY or keychain claude-memory/anthropic-api-key)."; exit 1; }

BID=$(python3 -c "import json;print(json.load(open('$PENDING'))['batch_id'])")
JSON=$(curl -fsS "https://api.anthropic.com/v1/messages/batches/$BID" \
        -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01")

python3 - "$JSON" <<'PY'
import json, sys, datetime
b = json.loads(sys.argv[1])
c = b.get("request_counts", {})
status = b.get("processing_status")
exp = b.get("expires_at", "")
tot = sum(c.values()) or 1
print(f"batch      {b['id']}")
print(f"status     {status}")
print(f"counts     succeeded={c.get('succeeded',0)} processing={c.get('processing',0)} "
      f"errored={c.get('errored',0)} expired={c.get('expired',0)} canceled={c.get('canceled',0)}")
print(f"expires_at {exp}")

# hours left (best-effort)
try:
    e = datetime.datetime.fromisoformat(exp.replace("Z","+00:00"))
    now = datetime.datetime.now(datetime.timezone.utc)
    hrs = (e - now).total_seconds()/3600
    print(f"time left  {hrs:.1f}h")
except Exception:
    hrs = None

print("\nRECOMMENDATION:")
if status == "ended" and c.get("succeeded",0) > 0:
    print("  ✓ Results ready. Pull them (no charge):")
    print("      python3 fetch_batch.py")
    print("      python3 build_dataset.py && python3 qa_dataset.py")
elif status in ("canceled",) or c.get("expired",0) or (status=="ended" and c.get("succeeded",0)==0):
    print("  ✗ Batch dead with no results. Recover — pick one:")
    print("      python3 extract_llm.py --batch     # cheap: resubmit a fresh 24h batch (50% off)")
    print("      python3 extract_llm.py             # sync: full price, but done in minutes, guaranteed")
elif hrs is not None and hrs < 2 and c.get("succeeded",0)==0:
    print("  ⚠ Still 0 with <2h left — likely to expire empty. Consider bailing to a guaranteed sync run NOW:")
    print("      python3 extract_llm.py             # sync, guaranteed (full price)")
    print("    (or gamble on the batch finishing in a final burst — poller catches it either way.)")
else:
    print("  … Still processing. The background poller (fetch_batch.py) will grab results the moment it ends.")
    print("    Re-check any time: ./batch_status.sh")
PY
