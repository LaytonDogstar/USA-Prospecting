"""Lead-buying strength: combine market signals into a ranking of who buys leads most.

Inputs (data/research/signals/):
  ahrefs_us_*.json          search traffic and paid-search spend per domain (Ahrefs)
  partner_lists.json        consent / partner lists published by lead-gen sites (who receives leads)
  acquisition_signals.json  disclosed marketing spend, partner share, affiliate programmes
  events_hiring.json        conference sponsorship and lead-acquisition hiring

Every signal is matched to a known company (prospect target or existing client) by website domain
or normalised name/brand. Unmatched names that appear on several partner lists are kept too: they
are buyers we don't yet know about.

Strength index (0-100), each part scaled against the strongest company in the set:
  partner lists 30 · organic search traffic 20 · paid-search spend 15 · disclosed lead buying 15
  · affiliate programme 10 · conferences 5 · hiring 5
"""
import json
import math
import re
from collections import defaultdict
from pathlib import Path

WEIGHTS = {"partner_lists": 30, "org_traffic": 20, "paid_spend": 15, "disclosed": 15,
           "affiliate": 10, "events": 5, "hiring": 5}
STOP = {"llc", "inc", "corp", "corporation", "co", "company", "ltd", "lp", "l", "p", "the", "holdings", "group",
        "financial", "finance", "services", "lending", "loans", "loan", "credit", "dba", "d", "b", "a", "of",
        "usa", "us", "com", "net", "org", "cash", "money"}


def norm(name: str) -> str:
    s = re.sub(r"https?://|www\.", "", (name or "").lower())
    s = re.sub(r"\.(com|net|org|cash|loans?|io|co)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w for w in s.split() if w not in STOP)


def domain(w: str | None) -> str | None:
    if not w:
        return None
    w = re.sub(r"^https?://", "", w.strip().lower()).split("/")[0].split()[0].strip(",;")
    return re.sub(r"^www\.", "", w) or None


class Matcher:
    """Map names and domains to known companies (targets and clients)."""

    def __init__(self, known: dict):
        self.by_domain, self.by_name = {}, {}
        for t in known["targets"]:
            key = ("target", t["company"])
            self._add(key, t["company"], t.get("website"), t.get("brands"))
        for c in known["clients"]:
            key = ("client", c["client"])
            self._add(key, c["client"], c.get("website"), None)

    def _add(self, key, name, website, brands):
        d = domain(website)
        if d:
            self.by_domain[d] = key
            self.by_name.setdefault(norm(d.split(".")[0]), key)
        n = norm(name)
        if n:
            self.by_name.setdefault(n, key)
        for b in re.split(r"[;,]", brands or ""):
            m = re.search(r"\(([^)]+\.[a-z]{2,})\)", b)
            if m:
                self.by_domain.setdefault(domain(m.group(1)), key)
            bn = norm(re.sub(r"\(.*?\)", "", b))
            if len(bn) >= 4:
                self.by_name.setdefault(bn, key)

    def match(self, name: str | None = None, website: str | None = None):
        d = domain(website)
        if d and d in self.by_domain:
            return self.by_domain[d]
        n = norm(name or "")
        if not n:
            return None
        if n in self.by_name:
            return self.by_name[n]
        squashed = n.replace(" ", "")
        for k, v in self.by_name.items():
            if len(k) >= 5 and k.replace(" ", "") == squashed:
                return v
        return None


def _load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def build(signals_dir: Path) -> list[dict]:
    known = _load(signals_dir / "known_companies.json", {"targets": [], "clients": []})
    m = Matcher(known)
    rows: dict = defaultdict(lambda: {"partner_lists": set(), "org_traffic": 0, "paid_spend": 0.0,
                                      "disclosed": [], "affiliate": [], "events": set(), "hiring": [],
                                      "aliases": set(), "domains": set()})

    def row(key):
        r = rows[key]
        r["kind"], r["name"] = key
        return r

    ahrefs_files = sorted(signals_dir.glob("ahrefs_us_*.json"))
    if ahrefs_files:
        data = _load(ahrefs_files[-1], {}).get("data", {})
        ahrefs_names = {d["domain"]: (d["kind"], d["name"]) for d in _load(signals_dir / "ahrefs_domains.json", [])}
        for d, (org, _paid, paid_cost, *_rest) in data.items():
            key = ahrefs_names.get(d) or m.match(website=d)
            if not key:
                continue
            r = row(key)
            r["org_traffic"] += org
            r["paid_spend"] += paid_cost / 100
            r["domains"].add(d)

    for lst in _load(signals_dir / "partner_lists.json", []):
        for p in lst.get("partners", []):
            key = m.match(name=p) or ("unmatched", norm(p) or p)
            r = row(key)
            r["partner_lists"].add(domain(lst.get("site")) or lst.get("site"))
            r["aliases"].add(p)

    for rec in _load(signals_dir / "acquisition_signals.json", []):
        key = m.match(name=rec.get("company"), website=rec.get("website"))
        if not key:
            continue
        r = row(key)
        for f in rec.get("facts", []):
            item = f"{f.get('field')}: {f.get('value')}" + (f" ({f['year']})" if f.get("year") else "")
            (r["affiliate"] if f.get("field", "").startswith("affiliate") else r["disclosed"]).append(item)

    eh = _load(signals_dir / "events_hiring.json", {})
    for ev in eh.get("events", []):
        for c in ev.get("companies", []):
            key = m.match(name=c.get("name"))
            if key:
                row(key)["events"].add(ev.get("event"))
    for h in eh.get("hiring", []):
        key = m.match(name=h.get("company"))
        if key:
            row(key)["hiring"].append(f"{h.get('role_title')} ({h.get('date') or 'undated'})")

    out = []
    for key, r in rows.items():
        if r["kind"] == "unmatched" and len(r["partner_lists"]) < 2:
            continue     # a name on one list only is noise
        out.append(r)
    if not out:
        return []
    mx = {
        "partner_lists": max(len(r["partner_lists"]) for r in out) or 1,
        "org_traffic": max(math.log1p(r["org_traffic"]) for r in out) or 1,
        "paid_spend": max(math.log1p(r["paid_spend"]) for r in out) or 1,
    }
    for r in out:
        parts = {
            "partner_lists": WEIGHTS["partner_lists"] * len(r["partner_lists"]) / mx["partner_lists"],
            "org_traffic": WEIGHTS["org_traffic"] * math.log1p(r["org_traffic"]) / mx["org_traffic"],
            "paid_spend": WEIGHTS["paid_spend"] * math.log1p(r["paid_spend"]) / mx["paid_spend"],
            "disclosed": WEIGHTS["disclosed"] if any(("partner_share" in d or "buys_leads" in d) for d in r["disclosed"]) else 0,
            "affiliate": WEIGHTS["affiliate"] if r["affiliate"] else 0,
            "events": WEIGHTS["events"] if r["events"] else 0,
            "hiring": WEIGHTS["hiring"] if r["hiring"] else 0,
        }
        r["parts"] = {k: round(v, 1) for k, v in parts.items()}
        r["strength"] = round(sum(parts.values()), 1)
        r["n_lists"] = len(r["partner_lists"])
        for k in ("partner_lists", "events", "aliases", "domains"):
            r[k] = sorted(x for x in r[k] if x)
    out.sort(key=lambda r: -r["strength"])
    return out
