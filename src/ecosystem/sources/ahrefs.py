"""Ahrefs API v3 client: domain metrics, SERP overview and plan usage. Budgeted and cached.

Ahrefs charges API units per row returned. The unit estimates below come from the live API (Nov 2026: domain
metrics about 50 units per call, SERP overview about 15 units per result row) and are used to check the budget
before a call. After each job the actual usage is read back from the free subscription endpoint and the
difference is recorded, so the budget tracks what Ahrefs really charged.
"""
import os
from datetime import date

import httpx

from .. import budget, cache

BASE = "https://api.ahrefs.com/v3"
METRICS_UNITS = 50
SERP_UNITS_PER_ROW = 15
CACHE_DAYS = 30


class AhrefsError(Exception):
    pass


class AhrefsClient:
    def __init__(self, s, api_key: str | None = None, job_run_id: int | None = None, http: httpx.Client | None = None):
        self.s = s
        self.key = api_key or os.environ.get("AHREFS_API_KEY")
        if not self.key:
            raise AhrefsError("AHREFS_API_KEY is not set")
        self.job_run_id = job_run_id
        self.http = http or httpx.Client(timeout=60)

    def _get(self, path: str, params: dict, estimate_units: float, force: bool = False):
        url = f"{BASE}/{path}"
        if not force:
            hit = cache.get(self.s, "ahrefs", url, params)
            if hit is not None:
                return hit, True
        if estimate_units:
            budget.check(self.s, "ahrefs", estimate_units)
        r = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"})
        if r.status_code == 401 or r.status_code == 403:
            raise AhrefsError(f"Ahrefs refused the request ({r.status_code}): check the API key and plan")
        if r.status_code >= 400:
            raise AhrefsError(f"Ahrefs error {r.status_code}: {r.text[:300]}")
        data = r.json()
        if estimate_units:
            budget.record(self.s, "ahrefs", estimate_units, 0, self.job_run_id)
        cache.put(self.s, "ahrefs", url, params, r.status_code, data, CACHE_DAYS)
        return data, False

    def subscription(self) -> dict:
        """Plan, limits and usage. Free: costs no units."""
        data, _ = self._get("subscription-info/limits-and-usage", {}, 0, force=True)
        return data.get("limits_and_usage", data)

    def domain_metrics(self, domain: str, country: str = "us", on: date | None = None, force: bool = False) -> dict:
        params = {"target": domain, "date": (on or date.today()).isoformat(), "mode": "subdomains", "country": country}
        data, cached = self._get("site-explorer/metrics", params, METRICS_UNITS, force)
        return {**data.get("metrics", {}), "_cached": cached}

    def serp(self, keyword: str, country: str = "us", top: int = 20, force: bool = False) -> dict:
        params = {"keyword": keyword, "country": country, "top_positions": top,
                  "select": "position,url,title,traffic,domain_rating,type,update_date"}
        data, cached = self._get("serp-overview/serp-overview", params, SERP_UNITS_PER_ROW * top, force)
        return {"positions": data.get("positions", []), "_cached": cached}
