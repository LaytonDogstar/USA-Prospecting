"""The working guide: how to use the portal, for everyone who can sign in."""
from fastapi import APIRouter, Depends, Request

from ..deps import admin_view, render

router = APIRouter()


@router.get("/guide")
def guide(request: Request, user=Depends(admin_view)):
    return render(request, "guide.html", user)
