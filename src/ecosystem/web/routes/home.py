from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..deps import current_user, get_db, render
from ..views import headline_counts, recent_activity, review_counts, top_unworked

router = APIRouter()

REVIEW_LABELS = {
    "possible_match": "Possible matches with our relationships",
    "dedup": "Possible duplicates",
    "new_entrant": "New entrants",
    "material_change": "Material changes",
    "low_confidence": "Unverified research",
    "parse_failure": "Cells that need reading by hand",
    "relationship_confirm": "Relationships to confirm",
    "manual_check": "Manual checks (e.g. licence lookups)",
    "research": "Research findings to accept",
}


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    counts = review_counts(db)
    buyers, held_b = top_unworked(db, "lenders")
    supply, held_s = top_unworked(db, "supply")
    from ... import notify
    needs = [n for n in notify.unread(db, user.username, limit=20) if n.level in ("problem", "action", "warning")][:5]
    return render(request, "home.html", user, needs=needs, buyers=buyers, supply=supply, held_b=held_b, held_s=held_s,
                  reviews=[(k, REVIEW_LABELS.get(k, k), counts[k]) for k in REVIEW_LABELS if counts.get(k)],
                  activity=recent_activity(db), head=headline_counts(db))
