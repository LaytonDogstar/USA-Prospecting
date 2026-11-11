"""Job runs: every enrichment or research run is logged with its inputs, cost, claims written and errors."""
from contextlib import contextmanager
from datetime import datetime, timezone

from . import models as m
from .budget import BudgetExceeded


class JobContext:
    def __init__(self, s, run: m.JobRun):
        self.s, self.run = s, run

    def error(self, message: str, **detail) -> None:
        self.run.errors = (self.run.errors or []) + [{"message": message[:500], **detail}]

    def wrote(self, n: int = 1) -> None:
        self.run.claims_written += n

    def cost(self, usd: float) -> None:
        self.run.cost_usd = float(self.run.cost_usd or 0) + usd


@contextmanager
def job(s, name: str, args: dict, actor: str):
    """Start a run, yield a context, and close it as done / budget_stopped / failed. Work done before a failure is
    kept: the run commits what it wrote, so nothing paid for is lost."""
    run = m.JobRun(job=name, args={**args, "started_by": actor}, status="running",
                   started_at=datetime.now(timezone.utc), errors=[], claims_written=0, cost_usd=0)
    s.add(run)
    s.flush()
    ctx = JobContext(s, run)
    try:
        yield ctx
        run.status = "done"
    except BudgetExceeded as e:
        run.status = "budget_stopped"
        ctx.error(str(e))
    except Exception as e:  # recorded on the run, then re-raised for the caller to see
        run.status = "failed"
        ctx.error(f"{type(e).__name__}: {e}")
        run.ended_at = datetime.now(timezone.utc)
        s.flush()
        raise
    finally:
        run.ended_at = run.ended_at or datetime.now(timezone.utc)
        s.flush()
