"""Command line entry point.

    python -m prospecting build --revenue data/raw/LenderTierLeadCount.xlsx \
        --filters data/raw/lender_tier_filters.csv --period "last 30 days to 2026-09-25"
"""
import argparse
import sys
from datetime import date
from pathlib import Path

from . import db
from .export import write_review
from .importers import append_reference_template, import_filters, import_revenue, load_reference
from .research import load_prospect_research, load_research


def cmd_build(args):
    conn = db.connect()
    reference = db.data_dir() / "reference" / "buyers.csv"

    rev = import_revenue(conn, Path(args.revenue), args.period)
    print(f"Revenue: {rev['rows']} tier rows loaded, {rev['duplicates_skipped']} duplicate rows skipped")
    fil = import_filters(conn, Path(args.filters))
    print(f"Filters: {fil['rows']} rows loaded, {fil['unnamed_tier_rows']} with no tier name")

    if reference.exists():
        print(f"Reference: {load_reference(conn, reference)['aliases']} aliases from {reference}")
    added = append_reference_template(conn, reference)
    if added:
        print(f"Reference: {added} new names added to {reference} with no category - fill them in and re-run")
        load_reference(conn, reference)

    research_files = sorted((db.data_dir() / "research").glob("*.json"))
    if research_files:
        r = load_research(conn, research_files)
        print(f"Research: {r['companies']} companies, {r['facts']} sourced facts from {len(research_files)} files "
              f"({r['skipped_no_source']} facts without a source skipped, {r['unmatched']} names not matched to a buyer)")

    out = Path(args.out) if args.out else db.output_dir() / f"client_profile_review_{date.today():%Y%m%d}.xlsx"
    res = write_review(conn, out, args.period, rev["duplicates_skipped"])
    print(f"Review workbook: {out} ({res['buyers']} buyers, {res['win_back']} win-back, {res['issues']} data issues)")


def cmd_targets(args):
    from .discover import SOURCE, build_universe
    from .export import write_targets
    conn = db.connect()
    if args.refresh or not conn.execute("SELECT 1 FROM company WHERE source = ? LIMIT 1", (SOURCE,)).fetchone():
        u = build_universe(conn, db.data_dir() / "reference" / "company_matches.csv", months=args.months)
        print(f"Universe: {u['companies']} companies from the CFPB database, {u['screened_out']} screened out, "
              f"{u['matched_to_buyers']} matched to existing buyers")
    files = sorted((db.data_dir() / "research" / "prospects").glob("*.json"))
    if files:
        r = load_prospect_research(conn, files, SOURCE)
        print(f"Prospect research: {r['companies']} companies, {r['facts']} sourced facts"
              + (f"; not matched: {r['unmatched']}" if r["unmatched"] else ""))
    out = Path(args.out) if args.out else db.output_dir() / f"targets_{date.today():%Y%m%d}.xlsx"
    res = write_targets(conn, out)
    print(f"Targets workbook: {out} ({res['targets']} scored targets, {res['universe']} companies in universe)")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="prospecting")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="Import exports and write the client profile review workbook")
    b.add_argument("--revenue", required=True, help="LenderTierLeadCount .xlsx export")
    b.add_argument("--filters", required=True, help="lender_tier_filters .csv export")
    b.add_argument("--period", required=True, help='Period the revenue covers, e.g. "last 30 days to 2026-09-25"')
    b.add_argument("--out", help="Output .xlsx path (default: output/client_profile_review_<date>.xlsx)")
    b.set_defaults(func=cmd_build)
    t = sub.add_parser("targets", help="Build the prospect universe (CFPB) and write the scored target list")
    t.add_argument("--refresh", action="store_true", help="Re-pull the CFPB universe even if already loaded")
    t.add_argument("--months", type=int, default=12, help="CFPB look-back window in months")
    t.add_argument("--out", help="Output .xlsx path (default: output/targets_<date>.xlsx)")
    t.set_defaults(func=cmd_targets)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
