#!/bin/bash
# Polls the Cobijo extraction batch until it leaves in_progress, then exits (0=done, 2=cap).
# Backgrounded so the harness re-invokes Claude on exit to run assembly.
BID="msgbatch_01UvJBixfg8as2HZKBMdViYq"
KEY=$(security find-generic-password -s claude-memory -a anthropic-api-key -w 2>/dev/null)
for i in $(seq 1 288); do   # 288 * 300s = 24h cap
  STATUS=$(curl -s "https://api.anthropic.com/v1/messages/batches/$BID" \
    -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['processing_status'])" 2>/dev/null)
  echo "[$(date +%H:%M)] poll $i: $STATUS"
  if [ "$STATUS" != "in_progress" ]; then echo "BATCH DONE: $STATUS"; exit 0; fi
  sleep 300
done
echo "poll cap reached, still in_progress"; exit 2
