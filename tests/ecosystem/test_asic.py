"""ASIC credit licence register → Australian lenders. Made-up licensees only."""
import csv

from sqlalchemy import select, text

from ecosystem import models as m
from ecosystem.asic import import_asic_licences, is_asic_file, kinds
from ecosystem.importers import detect_kind, import_relationship_seed
from ecosystem.matching import possible_match_sweep

from conftest import write_csv

COLS = ["REGISTER_NAME", "CRED_LIC_NUM", "CRED_LIC_NAME", "CRED_LIC_START_DT", "CRED_LIC_END_DT", "CRED_LIC_STATUS",
        "CRED_LIC_ABN_ACN", "CRED_LIC_AFSL_NUM", "CRED_LIC_STATUS_HISTORY", "CRED_LIC_LOCALITY", "CRED_LIC_STATE",
        "CRED_LIC_PCODE", "CRED_LIC_LAT", "CRED_LIC_LNG", "CRED_LIC_EDRS", "CRED_LIC_BN", "CRED_LIC_AUTHORISATIONS"]
PROVIDER = ("This licence authorises the licensee to:~Engage in credit activities as a credit provider by:~being a credit "
            "provider under a credit contract; and/or~carrying on a business of providing consumer leases; and/or")
BROKER = ("This licence authorises the licensee to:~Engage in credit activities other than as a credit provider by:~"
          "providing a credit service")
DEBT = "This licence authorises the licensee to:~Engage in credit activities:~by providing debt management services."


def _row(num, name, auth, status="APPR", bn="", hist=""):
    return ["Credit Licence", num, name, "01/02/2015", "", status, "12345678901", "", hist, "SYDNEY", "NSW", "2000",
            "", "", "AFCA", bn, auth]


def _file(tmp_path, name, rows):
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(COLS)
        w.writerows(rows)
    return path


def test_kinds():
    assert kinds(PROVIDER) == {"provider", "lessor"} and kinds(BROKER) == {"credit_service"}
    assert kinds(DEBT) == {"debt_management"}


def test_import_creates_au_lenders_and_refreshes(store, tmp_path):
    f1 = _file(tmp_path, "credit_lic_209901.csv", [
        _row("900001", "WREN CREDIT PTY LTD", PROVIDER, bn="WREN CASH"),
        _row("900002", "TEAL DEBT HELP PTY LTD", DEBT),
        _row("900003", "PLOVER MORTGAGES PTY LTD", BROKER),
        _row("900004", "STOAT FINANCE PTY LTD", PROVIDER, status="SUSP", hist="Suspended 01/01/2099"),
    ])
    assert detect_kind(f1) == "asic_credit_licence" and is_asic_file(f1)
    out = import_asic_licences(store, f1)
    assert out["imported"] == 3 and out["left_out_brokers"] == 1 and out["created"] == 3 and out["suspended"] == 1
    orgs = {o.legal_name: o for o in store.s.scalars(select(m.Organisation))}
    wren = orgs["WREN CREDIT PTY LTD"]
    assert wren.market == "AU" and wren.display_name == "WREN CASH" and wren.hq_country == "AU"
    roles = lambda o: {r.role for r in store.s.scalars(select(m.OrgRole).where(m.OrgRole.organisation_id == o.id))}
    assert roles(wren) == {"lender_au_acl"} and roles(orgs["TEAL DEBT HELP PTY LTD"]) == {"debt_relief"}
    assert "PLOVER MORTGAGES PTY LTD" not in orgs
    assert store.s.scalar(select(m.ComplianceFlag.flag_type).where(
        m.ComplianceFlag.entity_id == orgs["STOAT FINANCE PTY LTD"].id)) == "asic_licence"
    assert import_asic_licences(store, f1) == {"skipped": "already imported"}

    # next month: Teal left the register, Stoat is current again, Wren unchanged; matched by licence number
    f2 = _file(tmp_path, "credit_lic_209902.csv", [
        _row("900001", "WREN CREDIT PTY LTD", PROVIDER, bn="WREN CASH"),
        _row("900004", "STOAT FINANCE PTY LTD", PROVIDER),
    ])
    out = import_asic_licences(store, f2)
    assert out["created"] == 0 and out["unchanged"] == 1 and out["updated"] == 1 and out["no_longer_on_register"] == 1
    status = dict(store.s.execute(text("SELECT value->>'number', value->>'status' FROM entity_current "
                                       "WHERE attribute = 'asic_credit_licence'")).all())
    assert status == {"900001": "current", "900002": "not on register", "900004": "current"}


def test_au_partner_gets_a_possible_match_never_a_merge(store, tmp_path):
    seed = write_csv(tmp_path / "au.csv", ["name", "aliases", "domains", "role", "status", "notes", "market"],
                     [["Wren Cash", "", "", "buyer", "live", "AU buyer", "AU"]])
    import_relationship_seed(store, seed)
    import_asic_licences(store, _file(tmp_path, "credit_lic_209901.csv",
                                      [_row("900001", "WREN CREDIT PTY LTD", PROVIDER, bn="WREN CASH")]))
    assert store.s.scalar(select(m.Organisation.id).where(m.Organisation.legal_name == "WREN CREDIT PTY LTD"))
    possible_match_sweep(store)
    item = store.s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match"))
    assert item is not None and "Wren" in item.title


def _idr(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "How to interpret data"
    head = ["Firm Name", "ACN", "ABN", "AFS Licence Number", "Credit Licence Number", "Calendar Half Year",
            "Complaint Count"]
    fl = wb.create_sheet("Firm-level Metrics")
    fl.append(head)
    for half, n in (("2025 HY1", 500), ("2025 HY2", 40), ("2026 HY1", 60)):
        fl.append(["WREN CREDIT PTY LTD", "1", "1", None, "000900001", half, str(n)])
    fl.append(["SOME INSURER LTD", "2", "2", "000123", None, "2026 HY1", "999"])     # no credit licence: ignored
    pd = wb.create_sheet("Product Dimension")
    pd.append(["Firm Name", "ACN", "ABN", "AFS Licence Number", "Credit Licence Number", "Calendar Half Year",
               "Product Category", "Product Line", "Product", "Complaint Count"])
    pd.append(["WREN CREDIT PTY LTD", "1", "1", None, "000900001", "2026 HY1", "Credit", "Consumer credit",
               "Small amount credit contracts", "35"])
    pd.append(["WREN CREDIT PTY LTD", "1", "1", None, "000900001", "2025 HY2", "Credit", "Consumer credit",
               "Small amount credit contracts", "10"])
    pd.append(["WREN CREDIT PTY LTD", "1", "1", None, "000900001", "2025 HY1", "Credit", "Consumer credit",
               "Small amount credit contracts", "400"])   # older than 12 months
    path = tmp_path / "idr_data_file_20991116.xlsx"
    wb.save(path)
    return path


def test_idr_complaints_match_by_licence_number(store, tmp_path):
    from ecosystem.asic import import_idr_complaints, is_idr_file
    import_asic_licences(store, _file(tmp_path, "credit_lic_209901.csv",
                                     [_row("900001", "WREN CREDIT PTY LTD", PROVIDER, bn="WREN CASH")]))
    path = _idr(tmp_path)
    assert is_idr_file(path)
    out = import_idr_complaints(store, path)
    assert out["firms_matched"] == 1 and out["half_years"] == ["2025 HY2", "2026 HY1"]
    vals = dict(store.s.execute(text("SELECT metric, value FROM metric_snapshot")).all())
    assert float(vals["idr_complaints_12m"]) == 100 and float(vals["idr_complaints_sacc_12m"]) == 45
    from ecosystem.scoring import rescore
    rescore(store, audit=False)
    sc = store.s.scalar(select(m.Score).where(m.Score.score_type == "buyer_value"))
    assert "ASIC IDR" in sc.components["components"]["size"]["raw"]
    assert import_idr_complaints(store, path) == {"skipped": "already imported"}
