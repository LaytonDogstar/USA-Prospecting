import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ... import models as m
from ...parsing import US_STATES
from ...scoring import LENDER_ROLES, SUPPLY_ROLES
from ..deps import current_user, get_db, render
from ..views import OUR_STATUSES, ROLE_LABELS, org_table, people_table, tribes

router = APIRouter()
PAGE = 100
FILTER_KEYS = ("q", "role", "our", "compliance", "state", "tier", "min_score", "sort", "screened", "tribe", "group")


def _filters(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in FILTER_KEYS}


def _csv(rows: list[dict], columns: list[tuple[str, str]], name: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow([", ".join(r[k]) if isinstance(r.get(k), list) else r.get(k) for k, _ in columns])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}_{date.today()}.csv"'})


def _org_page(request: Request, db: Session, user, kind: str):
    f = _filters(request)
    if request.query_params.get("format") == "csv":
        rows, _ = org_table(db, kind, f, limit=None)
        cols = [("id", "id"), ("display_name", "name"), ("domain", "domain"), ("role_list", "types"),
                ("parent_name", "owner group"), ("tier", "tier"), ("score", "buyer value" if kind == "lenders" else "supply value"),
                ("lbs", "lead-buying strength"), ("sheet_score", "spreadsheet score"), ("loan_max", "max loan"),
                ("states_n", "states"), ("complaints", "CFPB complaints 12m"), ("organic", "organic visits/mo"),
                ("paid", "paid search $/mo"), ("our_status", "our relationship"), ("compliance", "compliance status"),
                ("flags", "compliance flags"), ("lifecycle", "lifecycle")]
        return _csv(rows, cols, kind)
    page = max(1, int(request.query_params.get("page") or 1))
    rows, total = org_table(db, kind, f, limit=PAGE, offset=(page - 1) * PAGE)
    roles = LENDER_ROLES if kind == "lenders" else SUPPLY_ROLES
    return render(request, "org_table.html", user, kind=kind, rows=rows, total=total, page=page, pages=(total - 1) // PAGE + 1,
                  f=f, roles=[(r, ROLE_LABELS[r]) for r in roles], our_statuses=OUR_STATUSES,
                  states=sorted(US_STATES), tribes=tribes(db) if kind == "lenders" else [])


@router.get("/lenders")
def lenders(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    return _org_page(request, db, user, "lenders")


@router.get("/supply")
def supply(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    return _org_page(request, db, user, "supply")


@router.get("/people")
def people(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    f = {"q": request.query_params.get("q", ""), "role_category": request.query_params.get("role_category", "")}
    if request.query_params.get("format") == "csv":
        rows = people_table(db, f, limit=None)
        cols = [("name", "name"), ("title", "title"), ("org_name", "organisation"), ("role_category", "role category"),
                ("email", "email"), ("linkedin_url", "LinkedIn"), ("source", "source"), ("last_verified_at", "last verified")]
        return _csv(rows, cols, "people")
    return render(request, "people.html", user, rows=people_table(db, f), f=f, role_categories=m.PERSON_ROLE_CATEGORIES)
