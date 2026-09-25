# US Partner Prospecting Tool

Profiles Dogstar's existing US lead buyers, works out what makes a buyer valuable, and (in later steps) finds and prioritises lookalike lenders to approach. The tool prepares lists; people do the outreach.

## Status

**Step 1 (this version):** import pingtree exports, merge buyer aliases, build buyer profiles, and write a review workbook.

Next: step 2, lookalike discovery against the reference profile.

## Data handling

- **No client, commercial or contact data goes into git.** Everything under `data/` and `output/` is git-ignored, as is `.env`.
- Keep buyer names out of code, tests and commit messages. Tests use made-up names.
- The contact tables record a source and a lawful basis per contact, because Dogstar Digital Ltd (UK) is the controller.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional; defaults to ./data and ./output
```

## Inputs (put in `data/`)

| File | What it is |
|---|---|
| `data/raw/LenderTierLeadCount_<date>.xlsx` | Pingtree tier report: one row per buyer tier, with sold/commission/EPL columns |
| `data/raw/lender_tier_filters_<date>.csv` | Tier filters, no header: buyer, tier, filter type, filter value |
| `data/reference/buyers.csv` | Maintained by hand: `alias, canonical_name, category, status, website, notes` |

In `buyers.csv`:
- **alias** is the name exactly as it appears in the exports.
- **canonical_name** merges several aliases into one company. Leave it blank to use the alias.
- **category** is `D` (direct lender), `S` (service provider), `N` (network) or `O` (non-lender offer).

Unknown names are appended to `buyers.csv` automatically on each build, with no category, so you can fill them in.

## Run

```bash
PYTHONPATH=src python3 -m prospecting build \
  --revenue data/raw/LenderTierLeadCount_2026-09-25.xlsx \
  --filters data/raw/lender_tier_filters_2026-09-25.csv \
  --period "last 30 days to 2026-09-25"
```

This writes `output/client_profile_review_<date>.xlsx` with these sheets:

| Sheet | Contents |
|---|---|
| Read me | Method and assumptions |
| Buyers | One row per buyer company: volume and revenue metrics, value tiers, filter profile |
| Reference profile | What the top buyers look like, as the basis for lookalike rules |
| Win-back | Direct buyers with no sales in the period |
| Data issues | Unknown names, missing filters, unnamed tiers |
| Tiers | Raw tier-level revenue |

The SQLite knowledge base is `data/prospecting.db`. Each build replaces the imported revenue and filter data.

## Value model

Buyers are scored on two measures:
- **Volume:** accepted leads
- **Price:** revenue

For each measure, buyers are sorted largest first and a running total is kept:
- **A** while the running total is within 80% of the total
- **B** while it is within 95%
- **C** for the rest

A buyer that is A on a measure is a reference for that segment (`volume`, `price` or `both`). Only categories D and S are tiered.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
