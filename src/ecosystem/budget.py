"""Monthly budgets per provider, enforced before every external call (brief §9.5).

Usage is recorded in `api_usage` as each call completes. A job asks `check()` with its estimate before calling;
if the estimate would take the provider over its monthly cap, `BudgetExceeded` is raised and the job stops.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import models as m

DEFAULT_LIMITS = {"anthropic": (300.0, "usd"), "ahrefs": (100_000.0, "units"), "similarweb": (0.0, "units")}


class BudgetExceeded(Exception):
    pass


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def limit(s, provider: str) -> tuple[float, str]:
    b = s.get(m.ProviderBudget, provider)
    if b is None:
        return DEFAULT_LIMITS[provider]
    return float(b.monthly_limit), b.unit


def set_limit(s, provider: str, value: float, actor: str) -> None:
    if provider not in m.PROVIDERS or value < 0:
        raise ValueError("unknown provider or negative limit")
    unit = DEFAULT_LIMITS[provider][1]
    b = s.get(m.ProviderBudget, provider)
    before = {"limit": float(b.monthly_limit)} if b else None
    if b is None:
        s.add(m.ProviderBudget(provider=provider, monthly_limit=value, unit=unit, updated_by=actor))
    else:
        b.monthly_limit, b.updated_by, b.updated_at = value, actor, datetime.now(timezone.utc)
    s.add(m.AuditLog(actor=actor, action="budget.set", table_name="provider_budget", before=before,
                     after={"provider": provider, "limit": value, "unit": unit}))
    s.flush()


def used(s, provider: str) -> float:
    """This calendar month's spend for the provider, in its budget unit."""
    _, unit = limit(s, provider)
    col = m.ApiUsage.cost_usd if unit == "usd" else m.ApiUsage.units
    return float(s.scalar(select(func.coalesce(func.sum(col), 0)).where(
        m.ApiUsage.provider == provider, m.ApiUsage.at >= month_start())) or 0)


def remaining(s, provider: str) -> float:
    lim, _ = limit(s, provider)
    return lim - used(s, provider)


def check(s, provider: str, estimate: float) -> None:
    lim, unit = limit(s, provider)
    if used(s, provider) + estimate > lim:
        raise BudgetExceeded(f"{provider} monthly budget reached: {used(s, provider):,.2f} of {lim:,.2f} {unit} used; "
                             f"this call needs about {estimate:,.2f} more")


def record(s, provider: str, units: float, cost_usd: float, job_run_id: int | None) -> None:
    s.add(m.ApiUsage(provider=provider, units=units, cost_usd=cost_usd, job_run_id=job_run_id))
    s.flush()


def summary(s) -> list[dict]:
    out = []
    for p in m.PROVIDERS:
        lim, unit = limit(s, p)
        u = used(s, p)
        out.append({"provider": p, "limit": lim, "unit": unit, "used": u, "remaining": lim - u,
                    "share": (u / lim) if lim else 0})
    return out
