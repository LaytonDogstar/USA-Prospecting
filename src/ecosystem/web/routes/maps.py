"""Ecosystem map (layered network) and state map."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...footprint import LENDER_TYPES, TYPE_LABELS, state_summary
from ...mapgraph import LAYERS, build_graph
from ...parsing import US_STATES
from ...scoring import LENDER_ROLES, SUPPLY_ROLES
from ..deps import current_user, get_db, render
from ..views import OUR_STATUSES, ROLE_LABELS, tribes

router = APIRouter()
MAP_FILTERS = ("view", "role", "tribe", "state", "our", "min_score", "compliance", "screened")


def _filters(request: Request) -> dict:
    return {k: request.query_params.get(k, "") for k in MAP_FILTERS}


def _expand(request: Request) -> set[int]:
    return {int(x) for x in request.query_params.get("expand", "").split(",") if x.strip().isdigit()}


@router.get("/map")
def map_page(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    return render(request, "map.html", user, f=_filters(request), expand=request.query_params.get("expand", ""),
                  roles=[(r, ROLE_LABELS[r]) for r in (*SUPPLY_ROLES, *LENDER_ROLES)], our_statuses=OUR_STATUSES,
                  states=sorted(US_STATES), tribes=tribes(db), layers=LAYERS)


@router.get("/map/data")
def map_data(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    return JSONResponse(build_graph(db, _filters(request), _expand(request)))


@router.get("/states")
def states_page(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    summary = state_summary(db)
    rows = sorted(summary["states"].items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    return render(request, "states.html", user, s=summary, rows=rows, names=US_STATES,
                  types=[k for k, _ in LENDER_TYPES] + ["unknown"], type_labels=TYPE_LABELS)
