"""Uploading files in the wrong order, then 'Reset and re-import', must end exactly like a clean import."""
from sqlalchemy import select

from ecosystem import config, models as m
from ecosystem.auth import create_user
from ecosystem.cli import import_all, reset_data
from ecosystem.importers import import_owner_groups, import_relationship_seed, import_targets, import_traffic
from ecosystem.reconcile import report


def _comparable(s):
    r = report(s)
    r.pop("import_files")
    r.pop("sources")  # spreadsheet sources are per row and import; counted elsewhere
    return r


def test_reset_after_wrong_order_matches_clean_import(store, tmp_path, monkeypatch, targets_file, traffic_file,
                                                      owner_groups_file, seed_file):
    raw, ref = tmp_path, tmp_path / "ref"
    ref.mkdir()
    owner = owner_groups_file.rename(ref / "owner_groups.csv")
    seed = seed_file.rename(ref / "our_relationships_seed.csv")
    monkeypatch.setattr(config, "RAW_DIR", raw)
    monkeypatch.setattr(config, "REFERENCE_DIR", ref)
    s = store.s
    create_user(s, "keeper", "a-long-password-1", "admin")

    # the reference: a clean import in the right order
    import_all(store)
    clean = _comparable(s)

    # start again, then upload in the wrong order (seeds and owner groups before the spreadsheets)
    from ecosystem.cli import DATA_TABLES
    from sqlalchemy import text
    s.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    store._url_sources.clear()
    import_relationship_seed(store, seed)
    import_owner_groups(store, owner)
    import_traffic(store, traffic_file)
    import_targets(store, targets_file)
    wrong = _comparable(s)
    assert wrong != clean  # the order really does change the result

    # reset fixes it
    out = reset_data(store)
    assert list(k for k in out if k.endswith((".xlsx", ".csv"))) == [
        "targets_20990101.xlsx", "CLE_test_traffic_options.xlsx", "owner_groups.csv", "our_relationships_seed.csv"]
    assert _comparable(s) == clean
    # users and the audit trail survive
    assert s.scalar(select(m.AppUser.username)) == "keeper"
    assert s.scalar(select(m.AuditLog).where(m.AuditLog.action == "data.reset")) is not None
