"""The working guide: how to use the portal, for everyone who can sign in."""
from fastapi import APIRouter, Depends, Request

from ..deps import current_user, render

router = APIRouter()


@router.get("/guide")
def guide(request: Request, user=Depends(current_user)):
    return render(request, "guide.html", user)
