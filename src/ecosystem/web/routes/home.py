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
}


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    counts = review_counts(db)
    return render(request, "home.html", user,
                  buyers=top_unworked(db, "lenders"), supply=top_unworked(db, "supply"),
                  reviews=[(k, REVIEW_LABELS.get(k, k), counts[k]) for k in REVIEW_LABELS if counts.get(k)],
                  activity=recent_activity(db), head=headline_counts(db))
