"""Admin: scoring weights and re-scoring, users, imports (upload), audit log."""
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m, scheduler
from ...auth import _ph, MIN_PASSWORD_LENGTH, create_user
from ...config import RAW_DIR, REFERENCE_DIR
from ...scoring import DEFAULT_WEIGHTS, current_weights, rescore, save_weights
from ...store import Store
from ..deps import Forbidden, admin_user, current_user, flash, get_db, render, store_for

router = APIRouter(prefix="/admin")
MAX_UPLOAD = 25 * 1024 * 1024
UPLOAD_RULES = [  # (filename pattern, folder)
    (re.compile(r"^targets_.+\.xlsx$"), RAW_DIR),
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
def _upload_dirs():
    from ... import config
    return config.RAW_DIR, config.REFERENCE_DIR


def _files_on_disk() -> list[str]:
    raw, ref = _upload_dirs()
    return sorted(p.name for d in (raw, ref) if d.exists() for p in d.iterdir()
                  if any(pat.match(p.name) for pat, _ in UPLOAD_RULES))


def _has_human_work(db: Session) -> bool:
    """Decisions or edits a reset would throw away."""
    return bool(db.scalar(select(m.ReviewItem.id).where(m.ReviewItem.status != "open").limit(1))
                or db.scalar(select(m.OurRelationship.id).where(m.OurRelationship.source == "manual").limit(1))
                or db.scalar(select(m.ComplianceStatus.entity_id).limit(1))
                or db.scalar(select(m.Person.id).limit(1)))


@router.get("/imports")
def imports_page(request: Request, db: Session = Depends(get_db), user=Depends(admin_view)):
    files = list(db.scalars(select(m.ImportFile).order_by(m.ImportFile.imported_at.desc(), m.ImportFile.id.desc())))
    from ...reconcile import report
    last_reset = db.scalar(select(m.AuditLog).where(m.AuditLog.action == "data.reset").order_by(m.AuditLog.at.desc()))
    on_disk = _files_on_disk()
    missing = [label for label, pat in (("the targets spreadsheet", UPLOAD_RULES[0][0]),
                                        ("the traffic options spreadsheet", UPLOAD_RULES[1][0]),
                                        ("owner_groups.csv", UPLOAD_RULES[2][0]),
                                        ("our_relationships_seed.csv", UPLOAD_RULES[3][0]))
               if not any(pat.match(n) for n in on_disk)]
    return render(request, "admin_imports.html", user, files=files, section="imports", on_disk=on_disk,
                  check=report(db), last_reset=last_reset, missing=missing,
                  patterns=["the targets workbook (.xlsx)", "the traffic options workbook (.xlsx)", "owner_groups.csv",
                            "our_relationships_seed.csv"])


@router.post("/imports")
async def imports_upload(request: Request, upload: UploadFile = File(...), db: Session = Depends(get_db),
                         user=Depends(admin_user)):
    from datetime import date

    from ...cli import import_all, reset_data
    from ...importers import IMPORT_ORDER, detect_kind
    raw, ref = _upload_dirs()
    original = Path(upload.filename or "upload").name
    raw.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=raw, suffix=".part") as tmp:
        size = 0
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                flash(request, "File too large (25 MB max).")
                return RedirectResponse("/admin/imports", status_code=303)
            tmp.write(chunk)
    tmp_path = Path(tmp.name)
    kind = detect_kind(tmp_path)
    if kind is None:
        tmp_path.unlink(missing_ok=True)
        flash(request, f"'{original}' wasn't recognised. Upload the targets workbook, the traffic options workbook, "
                       "owner_groups.csv or our_relationships_seed.csv.")
        return RedirectResponse("/admin/imports", status_code=303)
    # Store under the name the importer expects, whatever the file was called on the user's computer.
    stamp = (re.search(r"(20\d{6})", original) or [None, date.today().strftime("%Y%m%d")])[1]
    target = {"targets": raw / f"targets_{stamp}.xlsx",
              "traffic_options": raw / f"CLE_traffic_options_{stamp}.xlsx",
              "owner_groups": ref / "owner_groups.csv",
              "relationship_seed": ref / "our_relationships_seed.csv"}[kind]
    if kind in ("targets", "traffic_options"):
        same = [p for p in raw.glob("*.xlsx") if p != tmp_path and sha256_of(p) == sha256_of(tmp_path)]
        if same:
            tmp_path.unlink(missing_ok=True)
            flash(request, f"That spreadsheet is already uploaded (as {same[0].name}). Nothing changed.")
            return RedirectResponse("/admin/imports", status_code=303)
        n = 2
        while target.exists():  # a different file with the same date: keep both, never overwrite an original
            target = target.with_name(f"{target.stem.rsplit('_v', 1)[0]}_v{n}.xlsx")
            n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path), target)

    st = store_for(db, user)
    rank = IMPORT_ORDER.index(kind)
    later = [f.kind for f in db.scalars(select(m.ImportFile)) if f.kind in IMPORT_ORDER and IMPORT_ORDER.index(f.kind) > rank]
    if later and not _has_human_work(db):
        out = reset_data(st)  # re-import everything in the right order
        msg = (f"Saved {original} as {target.name}. It arrived after files that should come later, so everything "
               "was re-imported in the right order.")
    else:
        out = import_all(st)
        msg = f"Saved {original} as {target.name} and imported it. The sweeps and a re-score ran too."
        if later:
            msg += (" Note: it arrived after files that should come later and decisions have already been made, so it "
                    "was imported on top. Use 'Reset and re-import' if the results look wrong.")
    st.audit("import.upload", "import_file", None, None,
             {"file": original, "saved_as": target.name, "kind": kind, "result": {k: str(v)[:200] for k, v in out.items()}})
    missing = [k for k in IMPORT_ORDER if not db.scalar(select(m.ImportFile.id).where(m.ImportFile.kind == k).limit(1))]
    if missing:
        msg += " Still to upload: " + ", ".join(k.replace("_", " ") for k in missing) + "."
    flash(request, msg)
    return RedirectResponse("/admin/imports", status_code=303)


def sha256_of(path: Path) -> str:
    from ...importers import sha256
    return sha256(path)


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


# ---------------------------------------------------------------- research and enrichment (Phase 4)
JOBS = {"ahrefs_fill_traffic", "ahrefs_serp_snapshot", "research"}


def _session_factory():
    from sqlalchemy.orm import sessionmaker
    from ...db import engine
    return sessionmaker(bind=engine(), future=True)


def _run_job(name: str, actor: str, kwargs: dict) -> None:
    """Runs after the response (FastAPI background task) in its own session; the run is logged in job_run."""
    from ...scheduler import run_job
    s = _session_factory()()
    try:
        run_job(s, name, actor, kwargs)
        s.commit()
    except Exception:
        s.commit()  # keep the failed run's log and anything already paid for
    finally:
        s.close()


@router.get("/research")
def research_page(request: Request, db: Session = Depends(get_db), user=Depends(admin_view)):
    import os
    from ... import budget
    from ...research import DEFAULT_MODEL, PRICES
    runs = list(db.scalars(select(m.JobRun).order_by(m.JobRun.id.desc()).limit(25)))
    kws = list(db.scalars(select(m.TrackedKeyword).order_by(m.TrackedKeyword.keyword)))
    return render(request, "admin_research.html", user, section="research", budgets=budget.summary(db), runs=runs,
                  keywords=kws, keys={"anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                                      "ahrefs": bool(os.environ.get("AHREFS_API_KEY"))},
                  models=list(PRICES), default_model=DEFAULT_MODEL,
                  running={r.job for r in runs if r.status == "running"}, ahrefs_plan=request.session.pop("ahrefs_plan", None),
                  schedules=scheduler.ensure_defaults(db), schedule_defaults=scheduler.DEFAULTS,
                  scheduler_on=scheduler.enabled_by_env(),
                  env_keys={k: bool(os.environ.get(k)) for k in ("ANTHROPIC_API_KEY", "AHREFS_API_KEY")})


def _run_schedule_now(job: str, actor: str) -> None:
    s = _session_factory()()
    try:
        scheduler.run_schedule(s, s.get(m.JobSchedule, job), actor=actor)
        s.commit()
    except Exception:
        s.commit()
    finally:
        s.close()


@router.post("/research/schedule")
async def research_schedule(request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
                            user=Depends(admin_user)):
    form = await request.form()
    job = form.get("job")
    if job not in scheduler.DEFAULTS:
        raise HTTPException(400, "Unknown job")
    sc = next(x for x in scheduler.ensure_defaults(db) if x.job == job)
    before = {"enabled": sc.enabled, "every_hours": sc.every_hours, "params": sc.params}
    try:
        hours = int(form.get("every_hours") or sc.every_hours)
        params = scheduler.clean_params(job, {k[2:]: v for k, v in form.items() if k.startswith("p_") and v})
    except ValueError:
        flash(request, "Batch size and interval must be whole numbers.")
        return RedirectResponse("/admin/research#schedules", status_code=303)
    turning_on = bool(form.get("enabled")) and not sc.enabled
    sc.enabled, sc.every_hours, sc.params = bool(form.get("enabled")), max(1, min(hours, 720)), params
    sc.updated_by, sc.updated_at = user.username, scheduler.now()
    if turning_on and sc.next_run_at is None:
        sc.next_run_at = scheduler.now()
    st = store_for(db, user)
    st.audit("schedule.update", "job_schedule", None, before, {"job": job, "enabled": sc.enabled,
                                                               "every_hours": sc.every_hours, "params": sc.params})
    label = scheduler.DEFAULTS[job][0]
    if form.get("action") == "run":
        st.audit("job.start", "job_run", None, None, {"job": job, "via": "schedule run now", **params})
        db.commit()
        background.add_task(_run_schedule_now, job, user.username)
        flash(request, f"Started: {label}. Reload to follow it in Recent runs.")
    else:
        flash(request, f"{label}: {'on, every ' + str(sc.every_hours) + ' hours' if sc.enabled else 'off'}.")
    return RedirectResponse("/admin/research#schedules", status_code=303)


@router.post("/research/budget")
async def research_budget(request: Request, db: Session = Depends(get_db), user=Depends(admin_user)):
    from ... import budget
    form = await request.form()
    try:
        for p in m.PROVIDERS:
            if form.get(p) not in (None, ""):
                budget.set_limit(db, p, float(form.get(p)), user.username)
        flash(request, "Budgets saved.")
    except ValueError as e:
        flash(request, f"Not saved: {e}")
    return RedirectResponse("/admin/research", status_code=303)


@router.post("/research/keywords")
def research_keywords(request: Request, action: str = Form(...), keyword: str = Form(""), kid: int = Form(0),
                      db: Session = Depends(get_db), user=Depends(admin_user)):
    from ...enrich import seed_keywords
    if action == "seed":
        n = seed_keywords(db, user.username)
        flash(request, f"Added {n} starter keywords." if n else "Keywords already exist; nothing added.")
    elif action == "add" and keyword.strip():
        kw = keyword.strip().lower()[:120]
        if not db.scalar(select(m.TrackedKeyword.id).where(m.TrackedKeyword.keyword == kw)):
            db.add(m.TrackedKeyword(keyword=kw, active=True))
            store_for(db, user).audit("keywords.add", "tracked_keyword", None, None, {"keyword": kw})
        flash(request, f"Tracking '{kw}'.")
    elif action in ("pause", "resume") and kid:
        k = db.get(m.TrackedKeyword, kid)
        if k:
            k.active = action == "resume"
            store_for(db, user).audit(f"keywords.{action}", "tracked_keyword", kid, None, {"keyword": k.keyword})
    return RedirectResponse("/admin/research#keywords", status_code=303)


@router.post("/research/run")
def research_run(request: Request, background: BackgroundTasks, job_name: str = Form(...), n: int = Form(20),
                 model: str = Form(""), db: Session = Depends(get_db), user=Depends(admin_user)):
    import os
    from ...research import DEFAULT_MODEL, PRICES
    if job_name not in JOBS:
        raise HTTPException(400, "Unknown job")
    need = "ANTHROPIC_API_KEY" if job_name == "research" else "AHREFS_API_KEY"
    if not os.environ.get(need):
        flash(request, f"{need} isn't set in Railway yet, so this can't run.")
        return RedirectResponse("/admin/research", status_code=303)
    if db.scalar(select(m.JobRun.id).where(m.JobRun.job == job_name, m.JobRun.status == "running")):
        flash(request, "That job is already running. Wait for it to finish.")
        return RedirectResponse("/admin/research", status_code=303)
    n = max(1, min(n, 500))
    kwargs = {"ahrefs_fill_traffic": {"limit": n}, "ahrefs_serp_snapshot": {},
              "research": {"n": n, "model": model if model in PRICES else DEFAULT_MODEL}}[job_name]
    store_for(db, user).audit("job.start", "job_run", None, None, {"job": job_name, **kwargs})
    background.add_task(_run_job, job_name, user.username, kwargs)
    flash(request, f"Started {job_name.replace('_', ' ')}. Reload this page to follow it; results appear in the log below"
                   f"{' and research findings in the review inbox' if job_name == 'research' else ''}.")
    return RedirectResponse("/admin/research", status_code=303)


@router.post("/research/ahrefs-plan")
def research_ahrefs_plan(request: Request, db: Session = Depends(get_db), user=Depends(admin_user)):
    from ...sources.ahrefs import AhrefsClient, AhrefsError
    try:
        request.session["ahrefs_plan"] = AhrefsClient(db).subscription()
    except AhrefsError as e:
        flash(request, str(e))
    return RedirectResponse("/admin/research", status_code=303)
