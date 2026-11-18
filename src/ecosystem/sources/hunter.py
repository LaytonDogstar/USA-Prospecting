"""Hunter.io API v2 client: account (free), domain search, email finder and email verifier. Budgeted.

Hunter charges credits: a domain search or email finder that returns results costs 1, a verification 0.5. The
budget is checked before each call with those estimates and recorded after it. Results are not cached: contact
details go stale, and every call is a deliberate, budgeted lookup.

Only data Hunter returns through its API is used (it lists the public web pages where each address was found).
"""
import os

import httpx

from .. import budget

BASE = "https://api.hunter.io/v2"
SEARCH_CREDITS = 1.0
VERIFY_CREDITS = 0.5


class HunterError(Exception):
    pass


class HunterClient:
    def __init__(self, s, api_key: str | None = None, job_run_id: int | None = None, http: httpx.Client | None = None):
        self.s = s
        self.key = api_key or os.environ.get("HUNTER_API_KEY")
        if not self.key:
            raise HunterError("HUNTER_API_KEY is not set")
        self.job_run_id = job_run_id
        self.http = http or httpx.Client(timeout=60)

    def _get(self, path: str, params: dict, credits: float) -> dict:
        if credits:
            budget.check(self.s, "hunter", credits)
        r = self.http.get(f"{BASE}/{path}", params={**params, "api_key": self.key})
        if r.status_code in (401, 403):
            raise HunterError(f"Hunter refused the request ({r.status_code}): check the API key and plan")
        if r.status_code == 429:
            raise HunterError("Hunter rate limit or monthly quota reached")
        if r.status_code >= 400:
            try:
                detail = "; ".join(e.get("details", "") for e in r.json().get("errors", []))
            except ValueError:
                detail = r.text[:200]
            raise HunterError(f"Hunter error {r.status_code}: {detail[:300]}")
        data = r.json().get("data") or {}
        if credits:
            budget.record(self.s, "hunter", credits, 0, self.job_run_id)
        return data

    def account(self) -> dict:
        """Plan and remaining requests. Free."""
        return self._get("account", {}, 0)

    def domain_search(self, domain: str, limit: int = 10) -> dict:
        """People Hunter knows at a domain: email, name, position, department, seniority, confidence, sources."""
        return self._get("domain-search", {"domain": domain, "limit": limit, "type": "personal"}, SEARCH_CREDITS)

    def email_finder(self, domain: str, first_name: str, last_name: str) -> dict:
        return self._get("email-finder", {"domain": domain, "first_name": first_name, "last_name": last_name},
                         SEARCH_CREDITS)

    def verify(self, email: str) -> dict:
        """status: valid | invalid | accept_all | webmail | disposable | unknown; score 0-100."""
        return self._get("email-verifier", {"email": email}, VERIFY_CREDITS)
