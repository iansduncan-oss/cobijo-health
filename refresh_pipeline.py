#!/usr/bin/env python3
"""Weekly self-heal pipeline — keeps the served charity-care dataset current with zero human effort.

Runs ON the VPS (where the app reads data/ from disk), so a refresh needs no deploy from a dev box:

  scrape → freshness diff → re-extract only the changed hospitals → rebuild in place → restart → notify

Each stage reuses the same code paths a human would run by hand:
  1. hcai_lookup_scraper.py --out data/dataset_current.json   (the --out is REQUIRED; without it the
     scraper prints to stdout and the file never refreshes — the diff would then see no change)
  2. freshness_monitor.py                                     (offline URL/effective-date diff)
  3. scripts/reextract_changed.py prep                        (the changed hospitals -> source records)
     extract_llm.py --dataset … --out …                       (Opus; needs $ANTHROPIC_API_KEY in env)
     scripts/reextract_changed.py merge                       (splice + build_dataset.py, in place)
  4. systemctl restart cobijo-web                             (the app caches the dataset at startup)
  5. freshness_monitor.py --update                            (adopt current as the new baseline)

Safety rails:
  * CAP (--max, default 40): if MORE than this many hospitals changed in one week, that's almost
    certainly a scrape breakage or a mass HCAI re-publish, NOT real per-hospital policy change.
    We DO NOT auto-re-extract (would burn API $ and could publish garbage) and DO NOT adopt the
    baseline — we alert a human and stop. Real weekly drift is a handful of hospitals.
  * DEGENERATE-SCRAPE guard: if the scrape returns far fewer hospitals than the baseline (network
    blip / HCAI endpoint change → many spurious "removed"), abort WITHOUT adopting it as the baseline
    (which would blind next week's diff). Caps: --removed-max, or scraped count < 95% of baseline.
  * KEY PREFLIGHT: abort before the ~6min scrape if $ANTHROPIC_API_KEY is missing (can't re-extract).
  * AUTO-ROLLBACK: snapshot the live dataset before mutating; if the app is unhealthy after the
    restart (bad data won't boot), restore the snapshot + restart + re-check, and DON'T adopt the
    baseline — so the site never stays down and the change retries next week.
  * Re-extraction is authoritative-replace but the app's honesty layer still shows the needs_review
    caveat on any low-confidence row; the summary flags needs_review for a human spot-check.
  * Nothing destructive: needs_ocr / errored hospitals are HELD on their existing data, never blanked.
  * Idempotent: a run with no changes touches nothing and exits 0.

COST: the ONLY API spend is re-extracting the hospitals that genuinely changed (HCAI attachment URLs
are content-addressed stable GUIDs, so unchanged policies don't false-flag). Steady-state drift is a
handful/week; the --max cap bounds a bad week. Kept on SYNC Opus (not --batch): the changed set is
tiny so sync is faster wall-clock, and Batch's unbounded poll is a hang risk for an unattended cron —
the ~50% Batch saving is pennies on an already-negligible bill, not worth the reliability cost.

Notify: writes a summary to stdout (the cron redirects to /var/log/cobijo/freshness.log). If
RESEND_API_KEY + NOTIFY_EMAIL are set, also emails the summary. Exit codes: 0 = ok (changes or not),
2 = degenerate scrape / cap exceeded (human needed), 1 = a stage failed or a rollback happened.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import statutory_currency_check as _kuma          # reuse the hardened, anti-silence Kuma push helpers

ROOT = os.path.dirname(os.path.abspath(__file__))
REFRESH_KUMA_FILE = "/opt/cobijo/refresh_kuma_push_url"   # dedicated push monitor for the self-heal run
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
CURRENT = os.path.join(DATA, "dataset_current.json")
BASELINE = os.path.join(DATA, "freshness_baseline.json")
REPORT = os.path.join(OUT, "freshness_report.json")
CHANGED_SRC = os.path.join(DATA, "reextract_changed_source.json")
CHANGED_OUT = os.path.join(DATA, "extracted_changed.json")
SERVED = os.path.join(DATA, "cobijo_charity_care_dataset.json")   # what the app actually serves
FULL = os.path.join(DATA, "extracted_full.json")                  # source of truth build reads
BAK = os.path.join(DATA, ".selfheal_bak")                         # pre-refresh snapshot for rollback
PY = sys.executable
# Card generation needs Pillow + segno. Keep them OUT of the shared box's system python — a dedicated
# venv holds them; the pipeline uses it just for the gen scripts. On dev (no such venv) PY is used,
# where Pillow/segno already live. If neither has them, step 3b degrades to a warning.
GEN_PY = next((p for p in ("/opt/cobijo/venv/bin/python",) if os.path.exists(p)), PY)


def run(cmd, ok_codes=(0,), **kw):
    """Run a subprocess, capturing output; raise unless the exit code is in ok_codes."""
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)
    if r.returncode not in ok_codes:
        raise RuntimeError(f"{' '.join(cmd)} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def log(msg):
    print(msg, flush=True)


def notify(subject, body):
    """Best-effort email via Resend if configured; otherwise the log (cron) is the record."""
    key, to = os.environ.get("RESEND_API_KEY"), os.environ.get("NOTIFY_EMAIL")
    frm = os.environ.get("NOTIFY_FROM", "Cobijo Pipeline <pipeline@cobijohealth.org>")
    if not (key and to):
        # Neither set = email intentionally off (the cron log is the record) — stay quiet.
        # Exactly one set = a HALF-config: someone MEANT to enable failure alerts but botched it, and
        # would otherwise get silence (the same trap as the Kuma push-url bug). Say so loudly in the log.
        if key or to:
            log(f"  ⚠ email NOT sent: {'NOTIFY_EMAIL' if key else 'RESEND_API_KEY'} is unset "
                f"(set BOTH or NEITHER). This '{subject}' alert stayed in the log only.")
        return
    payload = json.dumps({"from": frm, "to": [to], "subject": subject,
                          "text": body}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=payload,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
        log("  (emailed summary)")
    except Exception as e:
        log(f"  (email failed: {type(e).__name__} — summary is in the log)")


def push_heartbeat(ok, msg):
    """Dead-man's-switch heartbeat to a dedicated Uptime-Kuma push monitor for the self-heal run.
    Complements notify() (email, best-effort, OFF on this box): a heartbeat also catches a DEAD cron —
    if the weekly run never fires, Kuma's interval lapses and alerts on its own, which no email can do.
    Reuses the currency check's anti-silence resolver so a misconfigured push-url file is loud, not silent.
    No-ops quietly when nothing is configured (the file simply isn't there), so it's safe to ship before
    the prod Kuma monitor exists."""
    url, warn = _kuma.resolve_kuma_url(env_var="COBIJO_REFRESH_KUMA_PUSH_URL",
                                       file_var="COBIJO_REFRESH_KUMA_PUSH_URL_FILE",
                                       default_file=REFRESH_KUMA_FILE)
    if warn:
        log(f"  ⚠ {warn}")
    if url:
        _kuma._kuma_push(url, ok, msg)


def _report_counts():
    rep = json.load(open(REPORT))
    return rep, len(rep.get("changed", [])) + len(rep.get("new", []))


def _hospital_counts():
    """(hospitals in the fresh scrape, hospitals in the baseline) — to catch a truncated scrape."""
    cur = len(json.load(open(CURRENT))) if os.path.exists(CURRENT) else 0
    base = len(json.load(open(BASELINE))) if os.path.exists(BASELINE) else 0
    return cur, base


def _healthy(port, tries=6, delay=1.5):
    """True once /healthz reports ok — the app caches the dataset at boot, so this confirms the
    REBUILT dataset actually loads. Retries because systemd restart isn't instant."""
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                if json.loads(r.read()).get("ok") is True:
                    return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def _snapshot(save):
    """save=True: back up the live dataset + source-of-truth BEFORE we mutate them, so a bad refresh
    can be rolled back. save=False: restore them. Only the two files the app + rebuild depend on."""
    os.makedirs(BAK, exist_ok=True)
    for path in (SERVED, FULL):
        src, dst = (path, os.path.join(BAK, os.path.basename(path)))
        a, b = (src, dst) if save else (dst, src)
        if os.path.exists(a):
            shutil.copy2(a, b)


def main():
    ap = argparse.ArgumentParser(description="Weekly self-heal: scrape→diff→re-extract→rebuild→restart")
    ap.add_argument("--max", type=int, default=40, help="abort auto-re-extract above this many changes")
    ap.add_argument("--no-restart", action="store_true", help="skip the systemctl restart (testing)")
    ap.add_argument("--dry-run", action="store_true", help="scrape + diff only; no extraction/restart")
    ap.add_argument("--skip-scrape", action="store_true",
                    help="reuse the existing data/dataset_current.json (skip the ~6min re-scrape)")
    ap.add_argument("--removed-max", type=int, default=15,
                    help="abort if MORE than this many hospitals vanished (a degenerate/truncated scrape)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("COBIJO_PORT", "8090")),
                    help="app port for the post-restart health check")
    args = ap.parse_args()

    log("=== cobijo self-heal pipeline ===")

    # 0. Preflight — fail BEFORE the ~6min scrape if we couldn't re-extract anyway (missing key). On
    #    the Linux VPS the env var is the only key source (the Keychain fallback is macOS-only).
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        msg = "ANTHROPIC_API_KEY not set — cannot re-extract. Aborting before the scrape."
        log("ABORT: " + msg)
        notify("⚠ Cobijo self-heal: no API key", msg)
        return 1

    # 1. Refresh the scrape (--out is mandatory — without it the scraper prints to stdout).
    if args.skip_scrape:
        log("[1/5] scrape SKIPPED (reusing existing dataset_current.json)")
    else:
        log("[1/5] scrape")
        run([PY, "hcai_lookup_scraper.py", "--out", "data/dataset_current.json"])

    # 2. Diff vs baseline.
    log("[2/5] freshness diff")
    run([PY, "freshness_monitor.py"], ok_codes=(0, 1))
    rep, n = _report_counts()
    removed = len(rep["removed"])
    cur_ct, base_ct = _hospital_counts()
    log(f"      changed={len(rep['changed'])} new={len(rep['new'])} removed={removed} "
        f"(scraped {cur_ct} vs baseline {base_ct})")

    # 2b. Degenerate-scrape guard: a network blip / HCAI endpoint change can return a short list, which
    #     shows up as many "removed" or a hospital count well below baseline. Processing it would waste
    #     API calls AND (worse) adopt the broken scrape as the new baseline, blinding next week's diff.
    if removed > args.removed_max or (base_ct and cur_ct < base_ct * 0.95):
        msg = (f"Scrape looks degenerate: {cur_ct} hospitals vs baseline {base_ct}, {removed} removed "
               f"(caps: removed>{args.removed_max} or count<95%). Likely a network/endpoint problem, "
               f"not real change. NOT processing; baseline left intact for a human to check.")
        log("ABORT: " + msg)
        notify("⚠ Cobijo self-heal: bad scrape — human review", msg)
        return 2

    if n == 0:
        log("No changes. Dataset already current — done.")
        return 0

    if n > args.max:
        msg = (f"{n} hospitals changed in one run (cap {args.max}). That's likely a scrape breakage "
               f"or a mass re-publish, not real per-hospital change. NOT auto-re-extracting; baseline "
               f"left as-is for a human to review. See output/freshness_report.json.")
        log(f"ABORT: {msg}")
        notify("⚠ Cobijo freshness: too many changes — human review", msg)
        return 2

    if args.dry_run:
        log(f"[dry-run] would re-extract {n} hospital(s); stopping before extraction.")
        return 0

    # 3. Snapshot the live data, then re-extract only the changed hospitals (Opus, sync — the changed
    #    set is tiny, so sync is cheaper in wall-clock + more reliable than an unattended Batch poll).
    _snapshot(save=True)
    log(f"[3/5] re-extract {n} changed hospital(s)")
    run([PY, "scripts/reextract_changed.py", "prep"])
    run([PY, "extract_llm.py", "--dataset", "data/reextract_changed_source.json",
         "--out", "data/extracted_changed.json"])
    merge = run([PY, "scripts/reextract_changed.py", "merge"])
    log(merge.stdout.strip())

    # 3b. Regenerate share cards — a re-extraction can RENAME a hospital (new slug), which then has no
    #     OG/QR card. Best-effort: a missing card just falls back to the generic site card, so never
    #     fail the data refresh over cosmetics (also needs Pillow/segno, which may be absent).
    log("[3b/5] regenerate OG + QR cards")
    for script in ("scripts/gen_og_images.py", "scripts/gen_qr_codes.py"):
        try:
            run([GEN_PY, script])
        except Exception as e:
            log(f"  WARN: {script} skipped ({str(e).splitlines()[0][:120]}) — a renamed hospital may "
                f"show the generic share card until cards are regenerated on dev.")

    # 4. Restart so the app picks up the rebuilt dataset, then HEALTH-CHECK. If the new data makes the
    #    app fail to boot, AUTO-ROLL BACK to the pre-refresh snapshot rather than leave the site down.
    if not args.no_restart:
        log("[4/5] restart cobijo-web")
        run(["sudo", "-n", "systemctl", "restart", "cobijo-web"])
        if not _healthy(args.port):
            log("  UNHEALTHY after restart — rolling back to the pre-refresh dataset")
            _snapshot(save=False)
            run(["sudo", "-n", "systemctl", "restart", "cobijo-web"])
            ok = _healthy(args.port)
            msg = ("A refresh made the app unhealthy; rolled back to the previous dataset. "
                   + ("It's healthy again — NO data update this week, baseline left intact so it retries."
                      if ok else "STILL UNHEALTHY after rollback — the site may be DOWN, needs a human NOW."))
            log("ABORT: " + msg)
            notify("⚠ Cobijo self-heal: rolled back" + ("" if ok else " — SITE MAY BE DOWN"), msg)
            return 1
        log("  healthy after restart")
    else:
        log("[4/5] restart skipped (--no-restart)")

    # 5. Adopt the new baseline so this week's changes don't re-flag next week. (Only reached on a
    #    healthy refresh — a rollback returns above WITHOUT adopting, so the change retries next week.)
    log("[5/5] adopt baseline")
    run([PY, "freshness_monitor.py", "--update"], ok_codes=(0, 1))

    # Summary + notify. The merge table (in the log) shows every before/after; flag needs_review.
    nr = sum(1 for r in json.load(open(CHANGED_OUT)) if r.get("needs_review"))
    summary = (f"Refreshed {n} hospital(s). "
               + (f"{nr} low-confidence (needs_review) — spot-check them. " if nr else "")
               + "Full before/after table in /var/log/cobijo/freshness.log.")
    log("DONE: " + summary)
    notify(f"Cobijo freshness: {n} hospital(s) refreshed", summary + "\n\n" + merge.stdout)
    return 0


_HEARTBEAT_MSG = {0: "self-heal ok", 1: "self-heal FAILED / rolled back — see freshness.log",
                  2: "self-heal degenerate scrape or cap exceeded — human needed"}


if __name__ == "__main__":
    real_run = "--dry-run" not in sys.argv          # a manual dry-run must not touch the weekly monitor
    try:
        code = main()
    except BaseException as e:                       # crash -> push DOWN now; Kuma's interval is the backstop
        if real_run:
            push_heartbeat(False, f"self-heal CRASHED: {type(e).__name__}")
        raise
    if real_run:
        push_heartbeat(code == 0, _HEARTBEAT_MSG.get(code, f"self-heal exit {code}"))
    sys.exit(code)
