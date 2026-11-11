"""Fixtures for the ecosystem tests. All company names here are made up.

Needs a Postgres database at TEST_DATABASE_URL (default postgresql://postgres@localhost:5433/ecosystem_test).
The schema is rebuilt from migrations for each test.
"""
import csv
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from ecosystem.config import database_url
from ecosystem.store import Store

URL = database_url(test=True)


@pytest.fixture(scope="session")
def migrated():
    from ecosystem.cli import migrate
    try:
        eng = create_engine(URL)
        with eng.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"test database not available: {e}")
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    migrate(URL)
    return eng


@pytest.fixture
def session(migrated):
    with migrated.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version'"))]
        c.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    s = sessionmaker(bind=migrated)()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def store(session):
    return Store(session, actor="test")


def write_xlsx(path: Path, sheets: dict[str, list[list]]) -> Path:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


TRAFFIC_HEADER = ["#", "Name", "Domain", "Category", "What it is", "Evidence", "Ahrefs est. US organic visits/mo",
                  "How to work with them", "Priority", "Source of evidence", "Notes / risk", "Status", "Owner", "Comments"]

UNIVERSE_HEADER = ["Company (CFPB name)", "Status", "Matched buyer", "Operator group", "Complaints (12m)", "Installment",
                   "Payday", "Line of credit"]

TARGETS_HEADER = ["Rank", "Company (CFPB name)", "Website", "Score", "Fit", "Lead-buying strength (0-100)",
                  "On partner lists (#)", "Organic visits/mo (Ahrefs)", "Paid search $/mo (Ahrefs)", "Type", "Why it fits",
                  "Operator group", "Consumer brands", "Parent / servicer", "Tribe", "Products", "Loan range", "States",
                  "Online / branch", "Segment", "Lead-buying evidence", "Litigation / regulatory", "CFPB complaints (12m)",
                  "Roles to approach", "Research confidence", "Sources", "Score: size", "Score: type", "Score: online",
                  "Score: lead signals", "Score: segment", "Decision (pursue / park / drop)", "Owner", "Your notes"]

LBS_HEADER = ["Rank", "Company", "Relationship", "Strength (0-100)", "Partner lists (#)", "Lists it appears on", "Names seen",
              "Domains sized", "Organic visits/mo", "Paid search $/mo", "Disclosed acquisition data", "Affiliate programme",
              "Conferences", "Lead-acquisition hiring", "Pts: lists", "Pts: organic", "Pts: paid", "Pts: disclosed",
              "Pts: affiliate", "Pts: events", "Pts: hiring"]


def targets_row(**kw) -> list:
    row = dict.fromkeys(TARGETS_HEADER)
    row.update(kw)
    return [row[h] for h in TARGETS_HEADER]


def lbs_row(**kw) -> list:
    row = dict.fromkeys(LBS_HEADER)
    row.update(kw)
    return [row[h] for h in LBS_HEADER]


@pytest.fixture
def traffic_file(tmp_path) -> Path:
    rows = [TRAFFIC_HEADER,
            [1, "Acme Ventures", "acmeventures.com", "1 Lead network / ping-tree", "Lead-gen group", "Site read", 1200,
             "Buy redirects", "A", "Site read 9 Nov", "One owner, many brands", "Not contacted", None, None],
            [2, "Rocket Bucks", "rocketbucks.com", "2 Connector / lead-gen brand", "Connector", "Ahrefs", 500,
             "Affiliate", "B", "Ahrefs data", None, "Not contacted", None, None],
            [3, "Jet Lend", "jetlend.com", "2 Connector / lead-gen brand", "Connector", "Ahrefs", None,
             "Affiliate", "B", "Ahrefs data", None, "Not contacted", None, None],
            [4, "Comet Cash", "—", "2 Connector / lead-gen brand", "Connector", "Earlier research this chat", None,
             "Affiliate", "C", "Earlier research this chat", None, "Not contacted", None, None],
            [5, "Orbit Money", "—", "2 Connector / lead-gen brand", "Connector", "Earlier research this chat", None,
             "Affiliate", "C", "Earlier research this chat", None, "Not contacted", None, None],
            [6, "Nova Funds", "—", "2 Connector / lead-gen brand", "Connector", "Earlier research this chat", None,
             "Affiliate", "C", "Earlier research this chat", None, "Not contacted", None, None],
            [7, "Pulse Network", "pulsenetwork.com", "1 Lead network / ping-tree", "Network", "Listing", None,
             "Reciprocal", "A", "Third-party listing", "Note FTC 2022 action — compliance review", "Not contacted", None, None],
            [8, "Quasar Loans", "quasarloans.com", "2 Connector / lead-gen brand", "Connector", "Site read", 90,
             "Affiliate", "B", "Site read 9 Nov", None, "Not contacted", None, None],
            [None] * 14,
            [None, "Legend: yellow columns are for the team.", None, None, None, None, None, None, None, None, None, None, None, None]]
    return write_xlsx(tmp_path / "CLE_test_traffic_options.xlsx",
                      {"All options": rows, "Summary": [["Category", "Total"]], "Read me": [["Item", "Detail"]]})


@pytest.fixture
def targets_file(tmp_path) -> Path:
    universe = [UNIVERSE_HEADER,
                ["Prairie Holdings, LLC", "candidate - researched", None, "Prairie Band / Plains Development Group", 40, "38", "2", None],
                ["Zephyr Tribal Lending Authority", "existing client / group", "Zephyr Loans", "Zephyr Band of Example Indians", 12, "12", None, None],
                ["Mega Bank NA", "screened out: bank or credit union", None, None, 300, "300", None, None],
                ["Quasar Lending, Inc.", "candidate - not yet researched", None, "POSSIBLE Nebula Group - check", 5, "5", None, None],
                ["Quasar Lending Company", "candidate - not yet researched", None, None, 2, "2", None, None]]
    targets = [TARGETS_HEADER,
               targets_row(**{
                   "Rank": 1, "Company (CFPB name)": "Prairie Holdings, LLC", "Website": "sunriselending.com", "Score": 80,
                   "Fit": "price", "Type": "tribal", "Operator group": "Prairie Band / Plains Development Group",
                   "Consumer brands": "Sunrise Lending (sunriselending.com) - Prairie Lending, LLC; "
                                      "Quick Cash Now (quickcashnowtest.com) - Plains Finance, LLC",
                   "Tribe": "Prairie Band of Example Indians", "Products": "installment loan",
                   "Loan range": "Sunrise: up to $1,200 new / $3,000 VIP", "States": "Not available in NY, PA, Vermont",
                   "Online / branch": "online-only", "Segment": "subprime",
                   "Litigation / regulatory": "Lawsuit alleges a rent-a-tribe scheme",
                   "Research confidence": "high",
                   "Sources": "https://sunriselending.com/\nhttps://news.example-times.com/story",
                   "Score: size": 20, "Score: type": 28})]
    lbs = [LBS_HEADER,
           lbs_row(Rank=1, Company="Prairie Holdings, LLC", Relationship="prospect", **{"Strength (0-100)": 30,
                   "Lists it appears on": "rocketbucks.com (+2 sites), listsite.com", "Pts: lists": 10}),
           lbs_row(Rank=2, Company="Zephyr Loans", Relationship="existing client", **{"Strength (0-100)": 20,
                   "Names seen": "Zephyr Loans, ZephyrLoans", "Domains sized": "zephyrloans.com"}),
           lbs_row(Rank=3, Company="nebulafinance", Relationship="not on our lists yet", **{"Strength (0-100)": 5,
                   "Names seen": "Nebula Finance, Nebula Finance LLC"})]
    return write_xlsx(tmp_path / "targets_20990101.xlsx",
                      {"Read me": [["Prospect targets"]], "Targets": targets, "Universe": universe,
                       "Lead-buying strength": lbs})


@pytest.fixture
def owner_groups_file(tmp_path) -> Path:
    rows = [["Acme Ventures", b, "owns", "inferred", "test", "operator_group"]
            for b in ("Rocket Bucks", "Jet Lend", "Comet Cash", "Orbit Money", "Nova Funds")]
    return write_csv(tmp_path / "owner_groups.csv", ["group", "member", "relation", "status", "note", "group_kind"], rows)


@pytest.fixture
def seed_file(tmp_path) -> Path:
    return write_csv(tmp_path / "our_relationships_seed.csv", ["name", "aliases", "domains", "role", "status", "notes"], [
        ["Pulse Network", "", "pulsenetwork.com", "network", "live", "test seed"],
        ["Quick Cash Now / Plains Finance", "Quick Cash Now|Plains Finance", "quickcashnowtest.com", "buyer", "contracting", "test seed"],
        ["Zephyr Loans", "", "", "buyer", "live", "confirmed"],
    ])
