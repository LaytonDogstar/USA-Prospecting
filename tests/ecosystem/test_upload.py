"""Uploads through Admin → Imports: files recognised by content (not name), and out-of-order uploads end up
identical to a clean import in the right order."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ecosystem import config, models as m
from ecosystem.auth import create_user
from ecosystem.cli import import_all
from ecosystem.reconcile import report
from ecosystem.store import Store
from ecosystem.web import app
from ecosystem.web.deps import get_db


def _comparable(s):
    r = report(s)
    r.pop("import_files"), r.pop("sources")
    r["rows_imported_per_sheet"] = {k: v for k, v in r["rows_imported_per_sheet"].items() if not k.endswith(".csv")}
    return r


@pytest.fixture
def client(session, migrated, tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    monkeypatch.setattr(config, "RAW_DIR", store_dir / "raw")
    monkeypatch.setattr(config, "REFERENCE_DIR", store_dir / "reference")
    create_user(session, "boss", "admin-password-1", "admin")
    session.commit()

    def _db():
        s = sessionmaker(bind=migrated)()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    c = TestClient(app)
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "boss", "password": "admin-password-1", "next": "/", "csrf": tok})
    yield c
    app.dependency_overrides.clear()


def upload(c, path, as_name):  # returns the page after redirect; flash shown in .flash
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/admin/imports").text).group(1)
    with open(path, "rb") as fh:
        return c.post("/admin/imports", data={"csrf": tok}, files={"upload": (as_name, fh)})


def test_wrong_order_and_odd_names_end_like_a_clean_import(client, session, migrated, tmp_path, targets_file,
                                                          traffic_file, owner_groups_file, seed_file):
    # reference result: clean import in the right order, in a separate scratch area
    ref_raw, ref_ref = tmp_path / "clean", tmp_path / "clean_ref"
    ref_raw.mkdir(), ref_ref.mkdir()
    import shutil
    shutil.copy(targets_file, ref_raw / targets_file.name)
    shutil.copy(traffic_file, ref_raw / traffic_file.name)
    shutil.copy(owner_groups_file, ref_ref / "owner_groups.csv")
    shutil.copy(seed_file, ref_ref / "our_relationships_seed.csv")
    st = Store(session, "test")
    import_all(st, raw=ref_raw, ref=ref_ref)
    session.commit()
    clean = _comparable(session)
    from ecosystem.cli import DATA_TABLES
    from sqlalchemy import text
    session.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    session.commit()

    # what happened on the live app: the CSVs first ...
    upload(client, owner_groups_file, "owner_groups.csv")
    r = upload(client, seed_file, "our_relationships_seed.csv")
    assert "Still to upload: targets, traffic options" in r.text
    assert "Not uploaded yet" in r.text
    # ... then the spreadsheets under names the old upload rejected
    r = upload(client, targets_file, "targets_20990101 (1).xlsx")
    assert "re-imported in the right order" in r.text and "targets_20990101.xlsx" in r.text
    r = upload(client, traffic_file, "Copy of traffic list.xlsx")
    assert "re-imported in the right order" in r.text
    assert "all four files uploaded" in r.text
    session.expire_all()
    assert _comparable(session) == clean
    # uploading the same spreadsheet again changes nothing
    r = upload(client, targets_file, "anything.xlsx")
    assert "already uploaded" in r.text


def test_unrecognised_file_is_refused(client, tmp_path):
    junk = tmp_path / "notes.csv"
    junk.write_text("a,b\n1,2\n")
    r = upload(client, junk, "notes.csv")
    assert "wasn&#39;t recognised" in r.text


def test_out_of_order_after_decisions_does_not_wipe_them(client, session, owner_groups_file, targets_file):
    upload(client, owner_groups_file, "owner_groups.csv")
    item = m.ReviewItem(kind="manual_check", dedupe_key="t1", title="decided", entity_ids=[], payload={},
                        status="approved", decided_by="boss")
    session.add(item)
    session.commit()
    r = upload(client, targets_file, "targets_20990101.xlsx")
    assert "decisions have already been made" in r.text
    session.expire_all()
    assert session.scalar(select(m.ReviewItem).where(m.ReviewItem.dedupe_key == "t1")) is not None
