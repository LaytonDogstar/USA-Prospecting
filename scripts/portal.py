"""Command-line client for the live Ecosystem portal, used by the scheduled Claude review sessions.

Reads ECOSYSTEM_URL (default the Railway URL), ECOSYSTEM_USERNAME and ECOSYSTEM_PASSWORD from the environment.
Never prints the password. Only reads, plus review decisions and "Run now" on a schedule: it has no commands for
users, budgets, weights, imports or resets.

  python scripts/portal.py status                 # budgets, schedules, recent runs, open counts (JSON)
  python scripts/portal.py report --hours 24      # the daily report (JSON)
  python scripts/portal.py review --kind possible_match --limit 50
  python scripts/portal.py decide 123 "link:45:67" --note "CFPB entity owns the brand (its site names it)"
  python scripts/portal.py undo-screen 141 --note "screened out on a failed search, 0 facts"
  python scripts/portal.py run-now assistant
"""
import argparse
import json
import os
import re
import sys

import httpx

URL = os.environ.get("ECOSYSTEM_URL", "https://usa-prospecting-production.up.railway.app").rstrip("/")
ACTION = re.compile(r"^(merge:\d+|link:\d+:\d+|approve|reject|screen)$")


class Portal:
    def __init__(self):
        user, pw = os.environ.get("ECOSYSTEM_USERNAME"), os.environ.get("ECOSYSTEM_PASSWORD")
        if not user or not pw:
            sys.exit("ECOSYSTEM_USERNAME / ECOSYSTEM_PASSWORD are not set in this environment.")
        self.c = httpx.Client(base_url=URL, timeout=120, follow_redirects=False)
        page = self.c.get("/login")
        tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = self.c.post("/login", data={"username": user, "password": pw, "next": "/", "csrf": tok})
        if r.status_code != 303:
            sys.exit("Login failed (wrong credentials, or the account is locked after 5 failures). Not retrying.")
        self.csrf = self.get("/api/csrf")["csrf"]

    def get(self, path, **params):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path, data):
        r = self.c.post(path, data={**data, "csrf": self.csrf})
        if r.status_code not in (200, 303):
            sys.exit(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("report"); p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("review"); p.add_argument("--kind", default=""); p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("decide"); p.add_argument("item", type=int); p.add_argument("action")
    p.add_argument("--note", required=True)
    p = sub.add_parser("undo-screen"); p.add_argument("item", type=int); p.add_argument("--note", required=True)
    p = sub.add_parser("run-now"); p.add_argument("job")
    a = ap.parse_args()
    pt = Portal()
    if a.cmd == "status":
        out = pt.get("/api/status")
    elif a.cmd == "report":
        out = pt.get("/api/report", hours=a.hours)
    elif a.cmd == "review":
        out = pt.get("/api/review", kind=a.kind, limit=a.limit, offset=a.offset)
    elif a.cmd == "decide":
        if not ACTION.match(a.action):
            sys.exit("action must be merge:<id>, link:<parent>:<child>, approve, reject or screen")
        r = pt.post(f"/review/{a.item}/decide", {"action": a.action, "note": a.note})
        out = {"item": a.item, "action": a.action, "status": r.status_code}
    elif a.cmd == "undo-screen":
        r = pt.post(f"/review/{a.item}/undo-screen", {"note": a.note})
        out = {"item": a.item, "undone": r.status_code == 303}
    else:
        r = pt.post(f"/admin/research/schedule/{a.job}/run", {})
        out = {"job": a.job, "started": r.status_code == 303}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
