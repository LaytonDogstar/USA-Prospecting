"""Command line: PYTHONPATH=src python -m ecosystem <command>. Run with --help for the list."""
import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from . import models as m
from .auth import create_user
from .config import RAW_DIR, REFERENCE_DIR
from .db import session_scope
from .importers import (default_attribute_defs, import_owner_groups, import_relationship_seed, import_targets,
                        import_traffic)
from .matching import name_dedup_suggestions, possible_match_sweep
from .reconcile import report, to_markdown
from .store import Store


def migrate(url: str | None = None) -> None:
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def import_all(store: Store, raw: Path = RAW_DIR, ref: Path = REFERENCE_DIR) -> dict:
    """The whole Phase 1 pipeline, in order. Each step is idempotent."""
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
    out["possible_match_sweep"] = possible_match_sweep(store)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ecosystem")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("import-all", help="import data/raw/*.xlsx and data/reference/*.csv, then run the sweeps")
    for name in ("import-targets", "import-traffic", "import-owner-groups", "import-seed"):
        sub.add_parser(name).add_argument("path", type=Path)
    sub.add_parser("sweep", help="raise possible matches against our relationships")
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
