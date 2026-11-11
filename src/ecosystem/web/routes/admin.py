"""Admin: scoring weights and re-scoring, users, imports (upload), audit log."""
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...auth import _ph, MIN_PASSWORD_LENGTH, create_user
from ...config import RAW_DIR, REFERENCE_DIR
from ...scoring import DEFAULT_WEIGHTS, current_weights, rescore, save_weights
from ..deps import Forbidden, admin_user, current_user, flash, get_db, render, store_for

router = APIRouter(prefix="/admin")
MAX_UPLOAD = 25 * 1024 * 1024
UPLOAD_RULES = [  # (filename pattern, folder)
    (re.compile(r"^targets_\d{8}\.xlsx$"), RAW_DIR),
    (re.compile(r"^CLE_.*traffic_options.*\.xlsx$"), RAW_DIR),
    (re.compile(r"^owner_groups\.csv$"), REFERENCE_DIR),
    (re.compile(r"^our_relationships_seed\.csv$"), REFERENCE_DIR),
]
COMPONENT_HELP = {
    "lead_buying": "Lead-buying strength index (partner lists, traffic, disclosures, affiliate programme, hiring)",
    "size": "Complaint volume, organic traffic and paid spend (log scale); originations once disclosed",
    "price_volume_fit": "Loan size vs our high-revenue and high-volume buyer profiles",
    "type_fit": "Servicer > tribal > bank-partner > CAB/CSO > state-licensed",
    "state_overlap": "States they lend in vs where our traffic comes from (needs internal data)",
    "segment": "Subprime highest, prime lowest",
    "traffic": "Estimated organic traffic (page-level for tables in Phase 4)",
    "relevance": "Subprime-loan intent of the supply category",
    "sheet_priority": "A/B/C priority from the traffic file",
    "ease": "Self-serve sign-up vs relationship deal",
    "owner_leverage": "How many brands one contact unlocks",
    "overlap": "Overlap with traffic we already receive (needs internal data)",
}


def admin_view(user=Depends(current_user)):
    if user.role != "admin":
        raise Forbidden("Admin pages are for admins only.")
    return user


@router.get("")
def admin_home():
    return RedirectResponse("/admin/weights", status_code=303)


@router.get("/weights")
def weights_page(request: Request, db: Session = Depends(get_db), user=Depends(admin_view)):
    data = {}
    for st in DEFAULT_WEIGHTS:
        cur = current_weights(db, st)
        history = list(db.scalars(select(m.WeightsVersion).where(m.WeightsVersion.score_type == st)
                                  .order_by(m.WeightsVersion.id.desc()).limit(10)))
        data[st] = {"current": cur, "history": history}
    runs = list(db.scalars(select(m.AuditLog).where(m.AuditLog.action == "scores.rescore").order_by(m.AuditLog.at.desc()).limit(5)))
    return render(request, "admin_weights.html", user, data=data, help=COMPONENT_HELP, runs=runs, section="weights")


@router.post("/weights/{score_type}")
async def save_weights_route(score_type: str, request: Request, db: Session = Depends(get_db), user=Depends(admin_user)):
    if score_type not in DEFAULT_WEIGHTS:
        raise HTTPException(404, "Unknown score type")
    form = await request.form()
    try:
        weights = {k: float(form.get(f"w_{k}", 0) or 0) for k in DEFAULT_WEIGHTS[score_type]}
        wv = save_weights(db, score_type, weights, user.username, str(form.get("note", "")))
    except ValueError as e:
        flash(request, f"Not saved: {e}")
        return RedirectResponse("/admin/weights", status_code=303)
    flash(request, f"Saved {score_type.replace('_', ' ')} weights as {wv.label}. Re-score to apply them.")
    return RedirectResponse("/admin/weights", status_code=303)


@router.post("/rescore")
def rescore_route(request: Request, db: Session = Depends(get_db), user=Depends(admin_user)):
    out = rescore(store_for(db, user))
    flash(request, "Re-scored: " + "; ".join(f"{k.replace('_', ' ')} {v['scored']} organisations ({v['weights']})"
                                             for k, v in out.items()))
    return RedirectResponse("/admin/weights", status_code=303)


# ---------------------------------------------------------------- users
@router.get("/users")
def users_page(request: Request, db: Session = Depends(get_db), user=Depends(admin_view)):
    users = list(db.scalars(select(m.AppUser).order_by(m.AppUser.username)))
    return render(request, "admin_users.html", user, users=users, min_len=MIN_PASSWORD_LENGTH, section="users")


@router.post("/users")
def users_create(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form("viewer"),
                 db: Session = Depends(get_db), user=Depends(admin_user)):
    try:
        create_user(db, username, password, role)
    except ValueError as e:
        flash(request, f"Not created: {e}")
        return RedirectResponse("/admin/users", status_code=303)
    store_for(db, user).audit("user.create", "app_user", None, None, {"username": username.lower(), "role": role})
    flash(request, f"Created {role} {username.lower()}.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{uid}")
def users_update(uid: int, request: Request, action: str = Form(...), password: str = Form(""), role: str = Form(""),
                 db: Session = Depends(get_db), user=Depends(admin_user)):
    u = db.get(m.AppUser, uid)
    if u is None:
        raise HTTPException(404)
    if u.id == user.id and action in ("deactivate", "role"):
        flash(request, "You can't deactivate yourself or change your own role.")
        return RedirectResponse("/admin/users", status_code=303)
    if action == "deactivate":
        u.is_active = False
    elif action == "activate":
        u.is_active, u.failed_logins, u.locked_until = True, 0, None
    elif action == "role" and role in m.USER_ROLES:
        u.role = role
    elif action == "password":
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(request, f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            return RedirectResponse("/admin/users", status_code=303)
        u.password_hash, u.failed_logins, u.locked_until = _ph.hash(password), 0, None
    else:
        raise HTTPException(400, "Unknown action")
    store_for(db, user).audit(f"user.{action}", "app_user", u.id, None, {"username": u.username, "role": u.role})
    flash(request, f"Updated {u.username}.")
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------- imports
@router.get("/imports")
def imports_page(request: Request, db: Session = Depends(get_db), user=Depends(admin_view)):
    files = list(db.scalars(select(m.ImportFile).order_by(m.ImportFile.imported_at.desc())))
    on_disk = sorted(p.name for d in (RAW_DIR, REFERENCE_DIR) if d.exists() for p in d.iterdir()
                     if any(pat.match(p.name) for pat, _ in UPLOAD_RULES))
    return render(request, "admin_imports.html", user, files=files, section="imports", on_disk=on_disk,
                  patterns=["targets_YYYYMMDD.xlsx", "CLE_…traffic_options….xlsx", "owner_groups.csv",
                            "our_relationships_seed.csv"])


@router.post("/imports")
async def imports_upload(request: Request, upload: UploadFile = File(...), db: Session = Depends(get_db),
                         user=Depends(admin_user)):
    name = Path(upload.filename or "").name
    folder = next((d for pat, d in UPLOAD_RULES if pat.match(name)), None)
    if folder is None:
        flash(request, f"'{name}' isn't a file name the importer knows. Expected one of: targets_YYYYMMDD.xlsx, "
                       "CLE_…traffic_options….xlsx, owner_groups.csv, our_relationships_seed.csv.")
        return RedirectResponse("/admin/imports", status_code=303)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=folder, suffix=".part") as tmp:
        size = 0
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                flash(request, "File too large (25 MB max).")
                return RedirectResponse("/admin/imports", status_code=303)
            tmp.write(chunk)
    target = folder / name
    if target.exists() and name.endswith(".xlsx"):
        flash(request, f"{name} is already uploaded. Spreadsheets are kept untouched; upload a new dated file instead.")
        Path(tmp.name).unlink(missing_ok=True)
        return RedirectResponse("/admin/imports", status_code=303)
    shutil.move(tmp.name, target)
    from ...cli import import_all
    st = store_for(db, user)
    out = import_all(st)
    rescore(st)
    st.audit("import.upload", "import_file", None, None, {"file": name, "result": {k: str(v)[:200] for k, v in out.items()}})
    flash(request, f"Uploaded {name}, ran the import, the sweeps and a re-score.")
    return RedirectResponse("/admin/imports", status_code=303)


@router.post("/reset")
def reset_route(request: Request, confirm: str = Form(""), db: Session = Depends(get_db), user=Depends(admin_user)):
    if confirm.strip() != "RESET":
        flash(request, "Not reset: type RESET in capitals to confirm.")
        return RedirectResponse("/admin/imports", status_code=303)
    from ...cli import reset_data
    out = reset_data(store_for(db, user))
    files = [k for k in out if k.endswith((".xlsx", ".csv"))]
    flash(request, f"Data reset and re-imported in order: {', '.join(files) or 'no files found'}.")
    return RedirectResponse("/admin/imports", status_code=303)


@router.get("/audit")
def audit_page(request: Request, db: Session = Depends(get_db), user=Depends(admin_view)):
    rows = list(db.scalars(select(m.AuditLog).order_by(m.AuditLog.at.desc(), m.AuditLog.id.desc()).limit(300)))
    return render(request, "admin_audit.html", user, rows=rows, section="audit")
