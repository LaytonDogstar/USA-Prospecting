"""Monthly budgets per provider and market, enforced before every external call (brief §9.5).

Usage is recorded in `api_usage` as each call completes. A job asks `check()` with its estimate before calling;
if the estimate would take the provider over its monthly cap, `BudgetExceeded` is raised and the job stops.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import models as m

DEFAULT_LIMITS = {"anthropic": (300.0, "usd"), "ahrefs": (100_000.0, "units"), "similarweb": (0.0, "units"),
                  "hunter": (50.0, "units")}  # Hunter credits: a search 1, a verification 0.5


class BudgetExceeded(Exception):
    pass


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _default(provider: str, market: str) -> tuple[float, str]:
    lim, unit = DEFAULT_LIMITS[provider]
    return (lim if market == "US" else 0.0), unit  # a new market spends nothing until an admin sets its budget


def limit(s, provider: str, market: str = "US") -> tuple[float, str]:
    b = s.get(m.ProviderBudget, (provider, market))
    if b is None:
        return _default(provider, market)
    return float(b.monthly_limit), b.unit


def set_limit(s, provider: str, value: float, actor: str, market: str = "US") -> None:
    if provider not in m.PROVIDERS or market not in m.MARKETS or value < 0:
        raise ValueError("unknown provider or market, or a negative limit")
    unit = DEFAULT_LIMITS[provider][1]
    b = s.get(m.ProviderBudget, (provider, market))
    before = {"limit": float(b.monthly_limit)} if b else None
    if b is None:
        s.add(m.ProviderBudget(provider=provider, market=market, monthly_limit=value, unit=unit, updated_by=actor))
    elif float(b.monthly_limit) == value:
        return
    else:
        b.monthly_limit, b.updated_by, b.updated_at = value, actor, datetime.now(timezone.utc)
    s.add(m.AuditLog(actor=actor, action="budget.set", table_name="provider_budget", before=before,
                     after={"provider": provider, "market": market, "limit": value, "unit": unit}))
    s.flush()


def used(s, provider: str, market: str = "US") -> float:
    """This calendar month's spend for the provider in one market, in its budget unit."""
    _, unit = limit(s, provider, market)
    col = m.ApiUsage.cost_usd if unit == "usd" else m.ApiUsage.units
    return float(s.scalar(select(func.coalesce(func.sum(col), 0)).where(
        m.ApiUsage.provider == provider, m.ApiUsage.market == market, m.ApiUsage.at >= month_start())) or 0)


def remaining(s, provider: str, market: str = "US") -> float:
    lim, _ = limit(s, provider, market)
    return lim - used(s, provider, market)


def check(s, provider: str, estimate: float, market: str = "US") -> None:
    lim, unit = limit(s, provider, market)
    u = used(s, provider, market)
    if u + estimate > lim:
        where = "" if market == "US" else f" ({m.MARKETS.get(market, market)})"
        raise BudgetExceeded(f"{provider}{where} monthly budget reached: {u:,.2f} of {lim:,.2f} {unit} used; "
                             f"this call needs about {estimate:,.2f} more")


def record(s, provider: str, units: float, cost_usd: float, job_run_id: int | None, market: str = "US") -> None:
    s.add(m.ApiUsage(provider=provider, market=market, units=units, cost_usd=cost_usd, job_run_id=job_run_id))
    s.flush()


def summary(s, markets=None) -> list[dict]:
    """One row per provider and market (US first)."""
    out = []
    for mk in (markets or m.MARKETS):
        for p in m.PROVIDERS:
            lim, unit = limit(s, p, mk)
            u = used(s, p, mk)
            out.append({"provider": p, "market": mk, "limit": lim, "unit": unit, "used": u, "remaining": lim - u,
                        "share": (u / lim) if lim else 0})
    return out
