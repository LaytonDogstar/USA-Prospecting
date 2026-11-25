"""Australia: ASIC's Credit Licensee Dataset (data.gov.au, monthly, CC BY 3.0 AU) → Australian lenders.

Only licensees that may *provide* credit or consumer leases, or provide debt management services, are imported (about a
quarter of the register); credit-service-only licensees (mostly mortgage and finance brokers) are left out. Each
licensee becomes an AU organisation (or is found again by its licence number on later files) with:

- an `asic_credit_licence` claim: number, status, start date, ABN/ACN, AFS licence, dispute scheme, what it may do;
- `lender_au_acl` (credit provider / lessor) and/or `debt_relief` (debt management) roles;
- its registered business name as an alias (and as the display name, since that is what people trade under);
- a compliance flag when the licence is suspended.

A licensee that has dropped off a newer file gets a claim saying so (cancelled or surrendered licences leave the register).
Matching to partners is left to the possible-match sweep: a licensee is never merged into a partner automatically.
"""
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select, text

from . import models as m
from .importers import begin
from .store import Store

METHOD = "import:asic:v1"
DATASET = "asic-credit-licensee"
CKAN = "https://data.gov.au/data/api/3/action/package_show"
HEADER_KEYS = {"cred_lic_num", "cred_lic_name", "cred_lic_authorisations"}
FILE_RE = re.compile(r"^credit_lic_.+\.csv$")


def kinds(authorisations: str) -> set[str]:
    """What the licence lets it do, from ASIC's authorisation text."""
    a = authorisations or ""
    k = set()
    if "Engage in credit activities as a credit provider" in a:
        k.add("provider")
    if "carrying on a business of providing consumer leases" in a:
        k.add("lessor")
    if "debt management services" in a:
        k.add("debt_management")
    if "other than as a credit provider" in a:
        k.add("credit_service")
    return k


def is_asic_file(path: Path) -> bool:
    try:
        header = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[0].lower()
    except Exception:
        return False
    return HEADER_KEYS <= {c.strip().strip('"') for c in header.split(",")}


def _date(v: str) -> str | None:
    try:
        return datetime.strptime(v.strip(), "%d/%m/%Y").date().isoformat()
    except (ValueError, AttributeError):
        return None


def _title(name: str) -> str:
    """ASIC names are upper case; the display name is tidied by friendly_name, so keep the original here."""
    return re.sub(r"\s+", " ", name or "").strip()


def import_asic_licences(store: Store, path: Path) -> dict:
    f = begin(store, path, "asic_credit_licence")
    if f is None:
        return {"skipped": "already imported"}
    s = store.s
    home_market, store.market = store.market, "AU"
    counts = {"rows": 0, "imported": 0, "created": 0, "updated": 0, "unchanged": 0, "suspended": 0,
              "left_out_brokers": 0, "no_longer_on_register": 0}
    # licence number -> organisation, from earlier files
    known = {str(num): oid for oid, num in s.execute(text(
        "SELECT entity_id, value->>'number' FROM entity_current WHERE attribute = 'asic_credit_licence'"))}
    current = {oid: val for oid, val in s.execute(text(
        "SELECT entity_id, value FROM entity_current WHERE attribute = 'asic_credit_licence'"))}
    seen = set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for i, r in enumerate(csv.DictReader(fh), start=2):
                counts["rows"] += 1
                k = kinds(r.get("CRED_LIC_AUTHORISATIONS", ""))
                if not k & {"provider", "lessor", "debt_management"}:
                    counts["left_out_brokers"] += 1
                    continue
                num = (r.get("CRED_LIC_NUM") or "").strip()
                name = _title(r.get("CRED_LIC_NAME", ""))
                if not num or not name:
                    continue
                seen.add(num)
                counts["imported"] += 1
                bn = _title(r.get("CRED_LIC_BN", ""))
                status = {"APPR": "current", "SUSP": "suspended"}.get((r.get("CRED_LIC_STATUS") or "").strip(),
                                                                       (r.get("CRED_LIC_STATUS") or "").strip().lower())
                value = {"number": num, "status": status, "start": _date(r.get("CRED_LIC_START_DT", "")),
                         "end": _date(r.get("CRED_LIC_END_DT", "")), "abn_acn": (r.get("CRED_LIC_ABN_ACN") or "").strip(),
                         "afsl": (r.get("CRED_LIC_AFSL_NUM") or "").strip() or None,
                         "dispute_scheme": (r.get("CRED_LIC_EDRS") or "").replace("~", ", ") or None,
                         "activities": sorted(k), "business_name": bn or None,
                         "status_history": (r.get("CRED_LIC_STATUS_HISTORY") or "").strip() or None}
                src = store.row_source(f, path.name, i)
                oid = known.get(num)
                if oid is None:
                    org = store.new_org(bn or name, legal_name=name, lifecycle="active")
                    org.hq_country = "AU"
                    counts["created"] += 1
                else:
                    org = store.org(oid)
                    if {kk: vv for kk, vv in (current.get(oid) or {}).items()} == value:
                        counts["unchanged"] += 1
                        continue
                    counts["updated"] += 1
                store.add_alias(org.id, name)
                if bn:
                    store.add_alias(org.id, bn)
                c = store.claim(org.id, "asic_credit_licence", value, src, confidence="high", method=METHOD)
                place = " ".join(x for x in ((r.get("CRED_LIC_LOCALITY") or "").title(), r.get("CRED_LIC_STATE") or "",
                                             r.get("CRED_LIC_PCODE") or "") if x).strip()
                if place:
                    store.claim(org.id, "hq_locality", place, src, confidence="high", method=METHOD)
                if k & {"provider", "lessor"}:
                    store.role(org.id, "lender_au_acl", c)
                if "debt_management" in k:
                    store.role(org.id, "debt_relief", c)
                if "lessor" in k:
                    store.claim(org.id, "consumer_leases", True, src, confidence="high", method=METHOD)
                if status == "suspended":
                    counts["suspended"] += 1
                    store.compliance_flag(org.id, "asic_licence",
                                          f"ASIC credit licence {num} suspended"
                                          + (f" ({value['status_history']})" if value["status_history"] else ""), c)
        # licensees from earlier files that have left the register (cancelled, surrendered or expired)
        gone_src = None
        for num, oid in known.items():
            if num in seen or (current.get(oid) or {}).get("status") == "not on register":
                continue
            if gone_src is None:
                gone_src = m.Source(source_type="regulator", title=f"{path.name}: not on the register",
                                    retrieved_at=datetime.now(timezone.utc), import_file_id=f.id)
                s.add(gone_src)
                s.flush()
            store.claim(oid, "asic_credit_licence", {**(current.get(oid) or {}), "status": "not on register"}, gone_src,
                        confidence="high", method=METHOD)
            counts["no_longer_on_register"] += 1
    finally:
        store.market = home_market
    f.row_counts = counts
    return counts


def fetch_latest(dest_dir: Path, http: httpx.Client | None = None) -> Path | None:
    """Download the current monthly CSV from data.gov.au into dest_dir (free). Returns the file, or None when the
    newest one is already there."""
    http = http or httpx.Client(timeout=120, follow_redirects=True)
    meta = http.get(CKAN, params={"id": DATASET}).json()
    res = next((r for r in meta["result"]["resources"]
                if (r.get("format") or "").upper() == "CSV" and "current" in (r.get("name") or "").lower()), None)
    if res is None:
        raise RuntimeError("the ASIC dataset has no current CSV resource")
    name = Path(res["url"]).name
    if not FILE_RE.match(name):
        name = f"credit_lic_{datetime.now(timezone.utc):%Y%m}.csv"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    if target.exists():
        return None
    tmp = target.with_suffix(".part")
    with http.stream("GET", res["url"]) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as out:
            for chunk in resp.iter_bytes(1 << 20):
                out.write(chunk)
    if not is_asic_file(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the downloaded file isn't the ASIC credit licence register")
    tmp.rename(target)
    return target


def refresh(store: Store, raw_dir: Path | None = None) -> dict:
    """The scheduled job: fetch this month's register and ASIC's complaints file if they are new, import them, then the
    partner check and a re-score of Australian organisations."""
    from . import config
    from .jobs import job
    from .matching import possible_match_sweep
    from .scoring import rescore
    raw_dir = raw_dir or config.RAW_DIR
    with job(store.s, "asic_register", {}, store.actor) as ctx:
        fetched = fetch_latest(raw_dir)
        out = {"fetched": fetched.name if fetched else None, "files": {}}
        for p in sorted(raw_dir.glob("credit_lic_*.csv")):
            out["files"][p.name] = import_asic_licences(store, p)
        try:  # complaints by firm (twice a year); a failure here doesn't undo the licence import
            idr = fetch_idr(raw_dir)
            out["fetched_idr"] = idr.name if idr else None
        except Exception as e:
            ctx.error(f"IDR complaints download failed: {e}")
        for p in sorted(raw_dir.glob("idr_data_file_*.xlsx"))[-1:]:  # the newest covers every half-year
            out["files"][p.name] = import_idr_complaints(store, p)
        au = set(store.s.scalars(select(m.Organisation.id).where(m.Organisation.market == "AU",
                                                                m.Organisation.lifecycle != "merged")))
        out["possible_match_sweep"] = possible_match_sweep(store, only=au)
        out["rescore"] = rescore(store, org_ids=au, audit=False)
        ctx.run.args = {**ctx.run.args, "result": out}
    return out


# ---------------------------------------------------------------- complaints (ASIC internal dispute resolution data)
IDR_PAGE = "https://asic.gov.au/regulatory-resources/financial-services/dispute-resolution/internal-dispute-resolution-data-dashboard/"
IDR_FILE_RE = re.compile(r"https://download\.asic\.gov\.au/files/idr/(idr_data_file_\d{8}\.xlsx)")
IDR_METHOD = "import:asic-idr:v1"
# product (Product Dimension, credit category) -> metric; the last 12 months = the two latest half-years in the file
IDR_PRODUCTS = {
    "Small amount credit contracts": "idr_complaints_sacc_12m",
    "Personal loans": "idr_complaints_personal_loans_12m",
    "Buy now, pay later": "idr_complaints_bnpl_12m",
    "Buy now, pay later (Low cost credit contract)": "idr_complaints_bnpl_12m",
    "Buy now, pay later (not Low cost credit contract)": "idr_complaints_bnpl_12m",
    "Debt management/credit repair": "idr_complaints_debt_management_12m",
    "Lines of credit/overdrafts": "idr_complaints_lines_of_credit_12m",
}


def _lic(v) -> str:
    return str(v or "").strip().lstrip("0")


def _int(v) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def is_idr_file(path: Path) -> bool:
    try:
        import openpyxl
        with open(path, "rb") as fh:
            names = set(openpyxl.load_workbook(fh, read_only=True).sheetnames)
        return {"Firm-level Metrics", "Product Dimension"} <= names
    except Exception:
        return False


def import_idr_complaints(store: Store, path: Path) -> dict:
    """Complaints each Australian lender received (all complaints, not only those escalated to AFCA), matched to
    the ASIC licensees by credit licence number. Writes 12-month metrics with a claim per firm."""
    import openpyxl
    f = begin(store, path, "asic_idr")
    if f is None:
        return {"skipped": "already imported"}
    s = store.s
    by_lic = {_lic(num): oid for oid, num in s.execute(text(
        "SELECT entity_id, value->>'number' FROM entity_current WHERE attribute = 'asic_credit_licence'"))}
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, read_only=True)
        firm_rows = list(wb["Firm-level Metrics"].iter_rows(values_only=True))
        head, firm_rows = [str(c) for c in firm_rows[0]], firm_rows[1:]
        col = {n: i for i, n in enumerate(head)}
        halves = sorted({str(r[col["Calendar Half Year"]]) for r in firm_rows if r[col["Calendar Half Year"]]})[-2:]
        totals, first_row, names = {}, {}, {}
        for i, r in enumerate(firm_rows, start=2):
            lic = _lic(r[col["Credit Licence Number"]])
            if not lic or lic not in by_lic or str(r[col["Calendar Half Year"]]) not in halves:
                continue
            totals[lic] = totals.get(lic, 0) + _int(r[col["Complaint Count"]])
            first_row.setdefault(lic, i)
            names[lic] = r[col["Firm Name"]]
        prod = {}
        rows = wb["Product Dimension"].iter_rows(values_only=True)
        phead = {n: i for i, n in enumerate(str(c) for c in next(rows))}
        for r in rows:
            lic = _lic(r[phead["Credit Licence Number"]])
            metric = IDR_PRODUCTS.get(r[phead["Product"]])
            if not metric or lic not in totals or str(r[phead["Calendar Half Year"]]) not in halves:
                continue
            prod[(lic, metric)] = prod.get((lic, metric), 0) + _int(r[phead["Complaint Count"]])
    period = f"{halves[0]} to {halves[-1]}" if halves else None
    counts = {"firms_matched": 0, "half_years": halves, "metrics": 0}
    for lic, total in totals.items():
        oid = by_lic[lic]
        src = store.row_source(f, "Firm-level Metrics", first_row[lic])
        values = {"idr_complaints_12m": total,
                  **{mt: n for (l2, mt), n in prod.items() if l2 == lic}}
        for metric, v in values.items():
            c = store.claim(oid, metric, v, src, confidence="high", method=IDR_METHOD)
            store.metric(oid, metric, v, period, src, c)
            counts["metrics"] += 1
        counts["firms_matched"] += 1
    f.row_counts = counts
    return counts


def fetch_idr(dest_dir: Path, http: httpx.Client | None = None) -> Path | None:
    """The data file linked from ASIC's IDR dashboard page (updated twice a year). None when already downloaded."""
    http = http or httpx.Client(timeout=300, follow_redirects=True)
    page = http.get(IDR_PAGE).text
    found = IDR_FILE_RE.search(page)
    if not found:
        raise RuntimeError("no IDR data file linked from ASIC's dashboard page")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / found.group(1)
    if target.exists():
        return None
    tmp = target.with_suffix(".part")
    with http.stream("GET", found.group(0)) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as out:
            for chunk in resp.iter_bytes(1 << 20):
                out.write(chunk)
    if not is_idr_file(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the downloaded file isn't ASIC's IDR data file")
    tmp.rename(target)
    return target
