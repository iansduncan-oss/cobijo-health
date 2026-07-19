#!/usr/bin/env python3
"""Statutory-currency monitor — alert a human when a PROGRAM-authority state's binding authority window has
passed, so the site's charity-care claims get re-verified instead of silently going stale.

Unlike a STATUTE (permanent until amended), a PROGRAM-authority state's guarantee (NC: the Medicaid HASP
charity-care condition) is renewed periodically and can lapse. The patient-facing copy is intentionally
EVERGREEN and fails safe, so the pages never assert a stale guarantee on their own. This out-of-band check
is the other half of the design: it watches each program state's `program_confirmed_through` date and pings
a human to RE-VERIFY the renewal — then bump the date (renewed) or flip `program_suspended` (confirmed lapse).

Runs in the same weekly prod cron as freshness_monitor.py. Exit code:
  0  all program states current (or only WARN — a heads-up before the window ends)
  1  at least one program state is STALE (past its confirmed window) or SUSPENDED  -> cron alerts

If COBIJO_KUMA_PUSH_URL is set, it ALSO pushes an Uptime-Kuma heartbeat: status=up when all clear,
status=down+msg when a re-verify is due. Kuma then fires your configured notification. Because it's a
heartbeat, a DEAD cron (no push at all) also trips the monitor — a dead-man's switch email/MAILTO can't give.

  COBIJO_KUMA_PUSH_URL="https://kuma.example/api/push/<token>" python3 statutory_currency_check.py

Read-only. The only network call is the optional Kuma GET. Cron (append to the existing cobijo weekly job):
  0 7 * * 1  cobijo  cd /opt/cobijo/app && COBIJO_KUMA_PUSH_URL="$(cat /opt/cobijo/kuma_push_url)" \
                     python3 statutory_currency_check.py >> /var/log/cobijo/currency.log 2>&1
"""
import datetime
import os
import sys
import urllib.parse
import urllib.request

import state_rules

WARN_DAYS = 45   # start nudging this many days BEFORE the confirmed window ends (proactive re-verify)


def evaluate(states, today):
    """Pure classification (no I/O) so it's unit-testable. Returns (rows, problems):
      rows     — [(code, status, days_left_or_None, watch_note)] for every program-authority state
      problems — the subset needing action now (STALE past the window, or SUSPENDED)
    A non-program state is skipped entirely (a statute doesn't expire)."""
    rows, problems = [], []
    for code, r in states.items():
        if not r.authority_is_program:
            continue
        if r.program_suspended:
            rows.append((code, "SUSPENDED", None, r.program_renewal_watch or ""))
            problems.append((code, "SUSPENDED", None))
            continue
        if not r.program_confirmed_through:
            rows.append((code, "UNTRACKED", None, "no program_confirmed_through set"))
            continue
        through = datetime.date.fromisoformat(r.program_confirmed_through)
        days = (through - today).days
        status = "STALE" if days < 0 else ("WARN" if days <= WARN_DAYS else "OK")
        rows.append((code, status, days, r.program_renewal_watch or ""))
        if status == "STALE":
            problems.append((code, status, days))
    return rows, problems


def _kuma_push(url, up, msg):
    """Fire one Uptime-Kuma push heartbeat. Returns True on HTTP 200."""
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}status={'up' if up else 'down'}&msg={urllib.parse.quote(msg)}"
    try:
        with urllib.request.urlopen(full, timeout=10) as resp:
            return getattr(resp, "status", resp.getcode()) == 200
    except Exception as e:                                   # network/URL error — never crash the cron
        print(f"kuma push failed: {e}", file=sys.stderr)
        return False


def main():
    today = datetime.date.today()
    rows, problems = evaluate(state_rules.STATES, today)
    for code, status, days, note in rows:
        d = "" if days is None else f" ({days:+d}d)"
        print(f"{status:9} {code}{d}  {note}")
    ok = not problems
    if problems:
        print("\n⚠️  RE-VERIFY program authority: " + ", ".join(f"{c}:{s}" for c, s, _ in problems))
    url = os.environ.get("COBIJO_KUMA_PUSH_URL")
    if url:
        msg = "all program states current" if ok else "re-verify: " + ", ".join(c for c, _, _ in problems)
        _kuma_push(url, ok, msg)
    elif not ok:
        print("(COBIJO_KUMA_PUSH_URL not set — alert stayed local; cron exit-1 is the only signal)", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
