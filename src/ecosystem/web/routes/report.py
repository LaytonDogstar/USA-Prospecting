"""Daily report: what ran, what it cost, what the assistant decided and what is waiting for a person."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...assistant import report
from ..deps import admin_view, get_db, render
from .home import REVIEW_LABELS

router = APIRouter()


@router.get("/report")
def daily_report(request: Request, hours: int = 24, db: Session = Depends(get_db), user=Depends(admin_view)):
    hours = hours if hours in (24, 72, 168) else 24
    return render(request, "report.html", user, r=report(db, hours), labels=REVIEW_LABELS, hours=hours)
