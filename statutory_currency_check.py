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


DEFAULT_KUMA_URL_FILE = "/opt/cobijo/kuma_push_url"


def resolve_kuma_url(environ=None, default_file=DEFAULT_KUMA_URL_FILE,
                     env_var="COBIJO_KUMA_PUSH_URL", file_var="COBIJO_KUMA_PUSH_URL_FILE"):
    """Resolve an Uptime-Kuma push URL, distinguishing 'not configured' (fine — stay quiet) from
    'configured but UNUSABLE' (loud). This closes the silent-failure that once disabled the heartbeat:
    the cron `cat`s the URL file into the env var, but when the file was unreadable by the cron user the
    export became an EMPTY string — indistinguishable from 'never configured', so main() took the benign
    'not set' branch and the alert quietly died. Now, if the env var is empty we look for the file
    ourselves: a present-but-unreadable/empty file yields a non-None WARNING instead of silence.

    env_var/file_var/default_file are parameterized so each monitor (currency check, self-heal pipeline)
    can point at its OWN Kuma push monitor while sharing this one anti-silence resolver.

    Returns (url, warning): url='' when there is genuinely nothing configured; warning is None normally,
    or a loud human-actionable string when a configured source exists but produced no usable URL."""
    env = os.environ if environ is None else environ
    url = (env.get(env_var) or "").strip()
    if url:
        return url, None
    path = env.get(file_var) or default_file
    if path and os.path.exists(path):
        try:
            data = open(path, encoding="utf-8").read().strip()
        except OSError as e:
            return "", (f"kuma: push-url file {path} exists but is UNREADABLE ({e.__class__.__name__}) — "
                        f"heartbeat DISABLED; fix ownership/perms so the cron user can read it")
        if not data:
            return "", f"kuma: push-url file {path} exists but is EMPTY — heartbeat DISABLED"
        return data, None
    return "", None                                     # genuinely unconfigured — silence is correct here


def main():
    today = datetime.date.today()
    rows, problems = evaluate(state_rules.STATES, today)
    for code, status, days, note in rows:
        d = "" if days is None else f" ({days:+d}d)"
        print(f"{status:9} {code}{d}  {note}")
    ok = not problems
    if problems:
        print("\n⚠️  RE-VERIFY program authority: " + ", ".join(f"{c}:{s}" for c, s, _ in problems))
    url, kuma_warn = resolve_kuma_url()
    if kuma_warn:                                       # never silent again: a broken alert path is loud + fails
        print(f"\n⚠️  {kuma_warn}", file=sys.stderr)
    if url:
        msg = "all program states current" if ok else "re-verify: " + ", ".join(c for c, _, _ in problems)
        _kuma_push(url, ok, msg)
    elif not ok:
        print("(no Kuma push URL configured — alert stayed local; cron exit-1 is the only signal)", file=sys.stderr)
    # exit nonzero if a program state needs re-verify OR the alert path itself is misconfigured
    sys.exit(0 if (ok and not kuma_warn) else 1)


if __name__ == "__main__":
    main()
