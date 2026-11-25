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


def test_australian_research_targets_prompt_budget_and_scoring(store, tmp_path):
    from types import SimpleNamespace as NS
    from ecosystem import budget
    from ecosystem.asic import import_idr_complaints
    from ecosystem.decisions import decide, suggest
    from ecosystem.research import pilot_targets, research_batch
    from ecosystem.scoring import rescore
    import_asic_licences(store, _file(tmp_path, "credit_lic_209901.csv", [
        _row("900001", "WREN CREDIT PTY LTD", PROVIDER, bn="WREN CASH"),
        _row("900005", "EXAMPLE SAVINGS BANK LIMITED", PROVIDER)]))
    import_idr_complaints(store, _idr(tmp_path))
    wren = store.s.scalar(select(m.Organisation).where(m.Organisation.legal_name == "WREN CREDIT PTY LTD"))
    picked = pilot_targets(store.s, 10, explain=True, market="AU")
    assert [o.id for o, _ in picked] == [wren.id] and "small-amount" in picked[0][1]  # the bank is left out
    assert wren.id not in {o.id for o in pilot_targets(store.s, 50)}  # never in the US queue

    seen = {}

    def create(**kw):
        seen.update(kw)
        page = NS(type="web_fetch_tool_result", model_dump=lambda: {"type": "web_fetch_tool_result",
                  "content": {"type": "web_fetch_result", "url": "https://www.wrencash.example.au/rates"}})
        rec = NS(type="tool_use", name="record_findings", id="t", input={
            "summary": "Wren Cash offers small amount credit contracts.", "is_us_consumer_lender": True,
            "lender_category": "small_amount", "answers": [],
            "findings": [{"attribute": f, "value": v, "source_url": "https://www.wrencash.example.au/rates", "quote": v,
                          "confidence": "high"} for f, v in (("lender_type_text", "small amount credit contracts"),
                                                             ("loan_range_text", "$200 - $2,000"))]})
        return NS(content=[page, rec], stop_reason="tool_use",
                  usage=NS(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=None))
    client = NS(beta=NS(messages=NS(create=create)))
    out = research_batch(store, n=5, client=client, market="AU")
    assert out["organisations"] == 0  # no Australian budget yet: nothing spent
    budget.set_limit(store.s, "anthropic", 5, "test", market="AU")
    out = research_batch(store, n=5, client=client, market="AU")
    assert out["organisations"] == 1
    assert "Australian consumer lenders" in seen["system"] and "ASIC" in seen["system"]
    enum = seen["tools"][-1]["input_schema"]["properties"]["lender_category"]["enum"]
    assert "small_amount" in enum and "tribal" not in enum
    assert budget.used(store.s, "anthropic", "AU") > 0 and budget.used(store.s, "anthropic") == 0
    it = store.s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "research"))
    sug = suggest(store, it, {})
    assert sug["action"] == "approve" and sug["safe"]
    decide(store, it, "approve", "test")
    rescore(store, audit=False)
    sc = store.s.scalar(select(m.Score).where(m.Score.entity_id == wren.id, m.Score.score_type == "buyer_value")
                        .order_by(m.Score.id.desc()))
    assert sc.components["components"]["type_fit"]["score"] == 100


def test_core_name_matches_partner_with_loans_suffix(store, tmp_path):
    from ecosystem.normalise import core_name
    assert core_name("Fundo Loans Pty Ltd") == core_name("Fundo") == "fundo"
    assert core_name("Cash Train") == "cashtrain" and core_name("Loans") == "" and core_name("Easy Finance") == ""
    seed = write_csv(tmp_path / "au.csv", ["name", "aliases", "domains", "role", "status", "notes", "market"],
                     [["Quoll", "", "", "buyer", "paused", "AU buyer", "AU"]])
    import_relationship_seed(store, seed)
    import_asic_licences(store, _file(tmp_path, "credit_lic_209901.csv",
                                      [_row("900007", "QUOLL LOANS PTY LTD", PROVIDER)]))
    possible_match_sweep(store)
    item = store.s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match"))
    assert item is not None and any("apart from words like Loans" in r for r in item.payload["reasons"])
    from ecosystem.research import pilot_targets
    assert not pilot_targets(store.s, 10, market="AU")  # held back from research until a person decides


def test_australian_keywords_and_search_snapshot(store):
    from ecosystem import budget
    from ecosystem.enrich import DEFAULT_KEYWORDS_AU, seed_keywords, serp_snapshot
    assert seed_keywords(store.s, "test", "AU") == len(DEFAULT_KEYWORDS_AU) and seed_keywords(store.s, "test", "AU") == 0
    assert seed_keywords(store.s, "test") > 0  # the US list is separate

    class FakeAhrefs:
        def __init__(self):
            self.calls, self.market, self.job_run_id = [], None, None

        def serp(self, kw, country="us", top=20, force=False):
            self.calls.append((kw, country))
            return {"positions": [{"url": "https://www.wombatloans.example.au/cash", "position": 1, "traffic": 900}]}

    budget.set_limit(store.s, "ahrefs", 1000, "test", market="AU")
    fake = FakeAhrefs()
    out = serp_snapshot(store, keywords=["centrelink loans"], client=fake, market="AU")
    assert fake.calls == [("centrelink loans", "au")] and fake.market == "AU" and out["new_domains"] == 1
    org = store.s.scalar(select(m.Organisation).join(m.Domain, m.Domain.organisation_id == m.Organisation.id)
                         .where(m.Domain.hostname == "wombatloans.example.au"))
    assert org.market == "AU" and store.market == "US"
    item = store.s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "new_entrant"))
    assert "Australia" in item.title
