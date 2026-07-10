#!/usr/bin/env python3
"""
Recovery tool — re-attach to an in-flight Anthropic message batch and write its results
into the per-corpus cache, WITHOUT re-submitting (no double charge).

Use this if the machine slept/rebooted and `extract_llm.py --batch`'s local poller died.
The batch itself keeps running on Anthropic's servers (results stored 29 days); this
reconnects by ID using the mapping saved at submit time (extract_cache/pending_batch.json).

After it finishes, run `python3 extract_llm.py --batch` to assemble extracted_full.json —
every corpus is now cached, so it submits 0 new requests and just maps + validates.

Usage:
  python3 fetch_batch.py                 # uses extract_cache/pending_batch.json
  python3 fetch_batch.py <batch_id>      # override the batch id
"""
import json
import os
import sys
import time
import urllib.request

import extract_llm as ex

PENDING = os.path.join(ex.CACHE_DIR, "pending_batch.json")


def main():
    if not os.path.exists(PENDING):
        sys.exit(f"No {PENDING}; nothing to resume. (It's written when a batch is submitted.)")
    meta = json.load(open(PENDING))
    batch_id = sys.argv[1] if len(sys.argv) > 1 else meta["batch_id"]
    id_to_sha = meta["id_to_sha"]
    truncated = meta.get("truncated", {})
    key = ex._api_key()
    if not key:
        sys.exit("No API key (env ANTHROPIC_API_KEY or keychain claude-memory/anthropic-api-key).")

    # Poll until the server-side batch is done.
    while True:
        b = ex._api_call("GET", f"{ex.BATCH_URL}/{batch_id}", key)
        counts = b.get("request_counts", {})
        if b.get("processing_status") == "ended":
            break
        print(f"  …processing {counts}", file=sys.stderr)
        time.sleep(30)
    print(f"  batch ended {counts}", file=sys.stderr)

    # Stream JSONL results into the per-sha cache.
    written = 0
    req = urllib.request.Request(
        b["results_url"], headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            item = json.loads(line)
            sha = id_to_sha.get(item.get("custom_id"))
            if not sha:
                continue
            res = item.get("result", {})
            if res.get("type") == "succeeded":
                inp = ex._tool_input(res.get("message", {}))
                out = inp if inp is not None else {"error": "no tool_use block"}
            else:
                out = {"error": f"batch_{res.get('type')}: {json.dumps(res.get('error') or res.get('type'))[:200]}"}
            if "error" not in out:
                out["_truncated"] = truncated.get(sha, False)
            json.dump(out, open(os.path.join(ex.CACHE_DIR, f"{sha}.json"), "w"),
                      indent=2, ensure_ascii=False)
            written += 1

    print(f"Wrote {written} results to {ex.CACHE_DIR}/. "
          f"Now run: python3 extract_llm.py --batch  (assembles extracted_full.json, submits 0).",
          file=sys.stderr)
    os.rename(PENDING, PENDING + ".done")  # mark consumed so it won't be reused


if __name__ == "__main__":
    main()
