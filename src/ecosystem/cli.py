"""Command line: PYTHONPATH=src python -m ecosystem <command>. Run with --help for the list."""
import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from . import models as m
from .auth import create_user
from .db import session_scope
from .importers import (default_attribute_defs, import_owner_groups, import_relationship_seed, import_targets,
                        import_traffic)
from .matching import name_dedup_suggestions, possible_match_sweep, shared_key_suggestions
from .reconcile import report, to_markdown
from .scoring import rescore
from .store import Store


def migrate(url: str | None = None) -> None:
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def import_all(store: Store, raw: Path | None = None, ref: Path | None = None) -> dict:
    """The whole Phase 1 pipeline, in order. Each step is idempotent."""
    from . import config
    raw, ref = raw or config.RAW_DIR, ref or config.REFERENCE_DIR
    out = {}
    default_attribute_defs(store)
    targets = sorted(raw.glob("targets_*.xlsx"))
    traffic = sorted(raw.glob("CLE_*traffic_options*.xlsx"))
    for path in targets:
        out[path.name] = import_targets(store, path)
    for path in traffic:
        out[path.name] = import_traffic(store, path)
    if (ref / "owner_groups.csv").exists():
        out["owner_groups.csv"] = import_owner_groups(store, ref / "owner_groups.csv")
    if (ref / "our_relationships_seed.csv").exists():
        out["our_relationships_seed.csv"] = import_relationship_seed(store, ref / "our_relationships_seed.csv")
    out["name_dedup_suggestions"] = name_dedup_suggestions(store)
    out["shared_key_suggestions"] = shared_key_suggestions(store)
    out["possible_match_sweep"] = possible_match_sweep(store)
    out["rescore"] = rescore(store)
    return out


# Everything built from imports and reviews. Kept: app_user, audit_log, weights_version, attribute_def,
# tracked_keyword and the Stage 2 job tables.
DATA_TABLES = (
    "review_item", "score", "compliance_status_history", "compliance_status", "compliance_flag", "metric_snapshot",
    "claim_source", "our_relationship_history", "our_relationship", "relationship", "entity_state", "org_role",
    "name_alias", "page", "person", "domain", "brand", "organisation", "claim", "source", "import_file", "entity",
)


def reset_data(store: Store) -> dict:
    """Wipe imported data and everything decided on it, then re-import the files on disk in the correct order.

    Users, scoring weights and the audit log are kept. The uploaded files themselves are not touched.
    """
    from sqlalchemy import text
    store.s.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    store.s.expire_all()          # drop ORM objects that pointed at the deleted rows
    store._url_sources.clear()    # and the per-import cache of URL sources
    store.audit("data.reset", None, None, None, {"note": "imported data cleared; re-importing files on disk"})
    return import_all(store)


def bootstrap_admin() -> str:
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD if there are no users yet (used on Railway)."""
    name, pw = os.environ.get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD")
    with session_scope() as s:
        if s.scalar(select(m.AppUser.id).limit(1)):
            return "users already exist; nothing to do"
        if not name or not pw:
            return "no users yet: set ADMIN_USERNAME and ADMIN_PASSWORD, or run create-user"
        try:
            create_user(s, name, pw, "admin")
        except ValueError as e:
            # Never stop the app from starting over a bad bootstrap value; say what to fix instead.
            return f"admin NOT created: {e}. Fix ADMIN_USERNAME / ADMIN_PASSWORD in the service variables."
        return f"created admin {name.lower()}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ecosystem")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("import-all", help="import data/raw/*.xlsx and data/reference/*.csv, then run the sweeps")
    for name in ("import-targets", "import-traffic", "import-owner-groups", "import-seed"):
        sub.add_parser(name).add_argument("path", type=Path)
    sub.add_parser("sweep", help="raise possible matches against our relationships")
    sub.add_parser("rescore", help="recompute buyer and supply value with the current weights")
    rs = sub.add_parser("reset-data", help="clear imported data and re-import the files on disk in order")
    rs.add_argument("--yes", action="store_true", help="required: confirms the reset")
    sub.add_parser("bootstrap-admin", help="create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD")
    sub.add_parser("ahrefs-plan", help="show the Ahrefs plan, limits and usage (free)")
    at = sub.add_parser("ahrefs-traffic", help="fill missing Ahrefs traffic figures")
    at.add_argument("--limit", type=int, default=50)
    sub.add_parser("ahrefs-serp", help="snapshot the top 20 results for each tracked keyword")
    rs2 = sub.add_parser("research", help="research pass on unresearched CFPB companies (findings go to Review)")
    rs2.add_argument("--n", type=int, default=20)
    rs2.add_argument("--model", default=None)
    bs = sub.add_parser("budget-set", help="set a provider's monthly budget")
    bs.add_argument("provider", choices=["anthropic", "ahrefs", "similarweb"])
    bs.add_argument("limit", type=float)
    rp = sub.add_parser("reconcile", help="print the count reconciliation")
    rp.add_argument("--json", action="store_true")
    rl = sub.add_parser("review-list")
    rl.add_argument("--kind")
    rl.add_argument("--limit", type=int, default=50)
    rd = sub.add_parser("review-decide", help="approve or reject a review item")
    rd.add_argument("id", type=int)
    rd.add_argument("decision", choices=["approve", "reject"])
    rd.add_argument("--merge-into", type=int, help="on approve: merge the other organisation into this one")
    rd.add_argument("--note", default="")
    rd.add_argument("--by", default=getpass.getuser())
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--role", default="viewer", choices=list(m.USER_ROLES))
    a = ap.parse_args(argv)

    if a.cmd == "migrate":
        migrate()
        return 0
    if a.cmd == "bootstrap-admin":
        print(bootstrap_admin())
        return 0
    if a.cmd == "create-user":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
        with session_scope() as s:
            create_user(s, a.username, pw, a.role)
        print(f"Created {a.role} {a.username}")
        return 0

    with session_scope() as s:
        store = Store(s, actor=f"cli:{getpass.getuser()}")
        if a.cmd == "import-all":
            print(json.dumps(import_all(store), indent=2, default=str))
            print(to_markdown(report(s)))
        elif a.cmd == "import-targets":
            print(import_targets(store, a.path))
        elif a.cmd == "import-traffic":
            print(import_traffic(store, a.path))
        elif a.cmd == "import-owner-groups":
            print(import_owner_groups(store, a.path))
        elif a.cmd == "import-seed":
            print(import_relationship_seed(store, a.path))
        elif a.cmd == "sweep":
            print(possible_match_sweep(store))
        elif a.cmd == "rescore":
            print(rescore(store))
        elif a.cmd == "ahrefs-plan":
            from .sources.ahrefs import AhrefsClient
            print(json.dumps(AhrefsClient(s).subscription(), indent=2))
        elif a.cmd == "ahrefs-traffic":
            from .enrich import fill_traffic
            print(fill_traffic(store, limit=a.limit))
        elif a.cmd == "ahrefs-serp":
            from .enrich import serp_snapshot
            print(serp_snapshot(store))
        elif a.cmd == "research":
            from .research import DEFAULT_MODEL, research_batch
            print(json.dumps(research_batch(store, n=a.n, model=a.model or DEFAULT_MODEL), indent=2))
        elif a.cmd == "budget-set":
            from .budget import set_limit
            set_limit(s, a.provider, a.limit, store.actor)
            print(f"{a.provider} monthly budget set to {a.limit:g}")
        elif a.cmd == "reset-data":
            if not a.yes:
                print("This clears all imported data, review decisions and relationship edits. Re-run with --yes.",
                      file=sys.stderr)
                return 1
            print(json.dumps(reset_data(store), indent=2, default=str))
        elif a.cmd == "reconcile":
            r = report(s)
            print(json.dumps(r, indent=2, default=str) if a.json else to_markdown(r))
        elif a.cmd == "review-list":
            q = select(m.ReviewItem).where(m.ReviewItem.status == "open").order_by(m.ReviewItem.kind, m.ReviewItem.id)
            if a.kind:
                q = q.where(m.ReviewItem.kind == a.kind)
            for it in s.scalars(q.limit(a.limit)):
                print(f"#{it.id} [{it.kind}] {it.title}  ids={it.entity_ids}  {json.dumps(it.payload.get('reasons', ''))}")
        elif a.cmd == "review-decide":
            it = s.get(m.ReviewItem, a.id)
            if it is None or it.status != "open":
                print("No open review item with that id", file=sys.stderr)
                return 1
            store.actor = a.by
            if a.decision == "approve" and a.merge_into:
                if a.merge_into not in it.entity_ids:
                    print("--merge-into must be one of the item's entity ids", file=sys.stderr)
                    return 1
                for other in it.entity_ids:
                    if other != a.merge_into:
                        store.merge(a.merge_into, other, note=f"review #{it.id}: {a.note}")
            it.status = "approved" if a.decision == "approve" else "rejected"
            it.decided_by, it.decided_at, it.note = a.by, datetime.now(timezone.utc), a.note
            store.audit(f"review.{a.decision}", "review_item", it.id, None, {"note": a.note, "merge_into": a.merge_into})
            print(f"#{it.id} {it.status}")
    return 0
