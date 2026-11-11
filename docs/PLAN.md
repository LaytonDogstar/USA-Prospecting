# Phase 0 plan: US Lead-Gen Industry Map ("Ecosystem")

Status: **approved 11 Nov 2026, with the decisions below.** Phase 1 (import and model) is built; see `CLAUDE.md`.

### Decisions (11 Nov 2026)
- **Blue Oak Credit / ServiceFintech is an existing client** (live), not in talks.
- **The 54 "existing client" names in the Lead-buying strength sheet are seeded as live, flagged "needs confirmation".** So are the brief's 9 seeds. The CFPB companies the Universe sheet matched to those buyers by hand get a `possible_match` review item, not a relationship.
- **Stack: Python (FastAPI + HTMX) as proposed.**
- **Login: username and password for now** (argon2 hashes, lockout after 5 failed attempts, admin/viewer roles). Google SSO can replace it later. This supersedes the SSO lines in §2, §9 and Q10–Q11.
Date: 11 Nov 2026. Based on the build brief, both spreadsheets and the existing code in this repo.

---

## 1. What I found in the data

I read both workbooks in full. The import has to handle these points.

### 1.1 Traffic file (`CLE_200_traffic_options.xlsx`)

| Check | Result |
|---|---|
| Rows in `All options` | 201, plus a legend row at the bottom that the importer must skip |
| Categories | 8, and the counts match the `Summary` sheet (21 / 35 / 29 / 29 / 31 / 22 / 9 / 25) |
| Priority | A 40, B 71, C 90 (matches `Summary`) |
| No domain (`—`) | 11 rows: Leap Theory, Acquire, PointClickTrack, Paradise Media, PayDay Mansion, Big Buck Loans, Loan Raptor, Fund Finance, HelpingHand Payday Loans, Zeydoo, MyLead |
| Ahrefs traffic filled | 84 of 201 |
| "Earlier research this chat" | 31 rows. These become claims with confidence `low`, flagged for review |
| Status / Owner / Comments | All "Not contacted" and empty. Nothing to carry into `our_relationships` |

Owner groups in its Read me: Pay Day Ventures (5 brands), Selling Source → MoneyMutual, PointClickTrack → CashUSA / PersonalLoans / BadCreditLoans, Heart Paydays ≈ Credit Clock (shared template, so `same_owner_as`, inferred), Credit Clock offered via Lead Stack Media, LendYou → mypayday.com, Paradise Media → local news sites.

Three of the Pay Day Ventures brands (Big Buck Loans, Loan Raptor, Fund Finance) have no domain. They can only be grouped from the Read me text, so the importer has an explicit owner-group seed list taken from both Read me sheets, not a text parser.

### 1.2 Targets file (`targets_20260929.xlsx`)

| Sheet | Rows | Notes |
|---|---|---|
| Universe | 726 | 453 not yet researched, 65 researched, 23 "existing client / group", 185 screened out (about 25 reasons) |
| Targets | 65 | All 65 are in Universe. 57 have a website. Research confidence: 45 high, 10 medium, 10 low. About 3 source URLs per row |
| Lead-buying strength | 685 | 54 "existing client", 56 "prospect", 575 "not on our lists yet". **628 of the 685 names are not CFPB names**: they are keys and aliases taken from partner and consent lists (e.g. `60monthloans`, "Loan Me, LoanMe"). Only 108 rows have a domain |

Overlap across files is small: 4 domains are in both Targets and the traffic file (opploans.com, cashnetusa.com, checkintocash.com, xact.com) and 6 are in both Lead-buying strength and the traffic file. Most dedup work is **inside** the targets file, between CFPB legal names and the list-derived names in Lead-buying strength.

Rough size: about 1,500 rows feed in. I expect about 1,250–1,350 organisations after dedup. Phase 1 will report the exact numbers and reconcile them against each sheet.

### 1.3 Problems the import must fix or flag

1. **GVA Holdings (Targets rank #1)** has no matched buyer and is marked "prospect" in both Targets and Lead-buying strength. Its `Consumer brands` cell names Cash Advance Now (cashadvancenow.com) / GreatPlains Finance, LLC. Once `our_relationships` holds Cash Advance Now with that domain, the match is caught by domain and by brand name. This is acceptance test #1.
2. **Blue Oak Credit conflict.** The brief says "in talks". The targets file says "existing client" (Lead-buying strength), and Universe maps Kashia Services (Kashia Band of Pomo Indians) to Blue Oak Credit as "existing client / group". See question Q8.
3. **Spreadsheet client labels are name matches.** 23 Universe rows and 54 Lead-buying strength rows say "existing client". Following §6, none of these create `our_relationships` rows. Each becomes a **possible match** review item that shows the spreadsheet's claim, so the team can confirm or reject it in one click.
4. **Free-text fields.** Type, Segment, Loan range, States and Online/branch are free text (e.g. "state-licensed (CashNetUSA) and bank-partner (NetCredit via Quill Bank…); CAB in Texas"). The importer stores the original text as a claim and adds parsed values (roles, loan_min/max, state list) as separate claims with `method = parse:v1`. Rows it can't parse go to the review inbox, so it never guesses quietly.
5. **Operator group strings with hedges**, e.g. "Heights Finance (inferred)" and "POSSIBLE Otoe-Missouria (your Caliber WithU group) - check". These become `inferred` or `rumoured` edges, never `confirmed`.

---

## 2. Stack

**Recommendation: Python throughout.** FastAPI, server-rendered pages (Jinja + HTMX), Postgres, and two small JavaScript parts: Cytoscape.js for the map and a D3 US map for states.

Why not Next.js + React:
- The repo is already Python. CFPB discovery, the polite fetcher (robots.txt, per-host rate limit), lead-buying signals and scoring all port directly.
- The workers (imports, research, scoring) are Python, so the web app and the jobs share one codebase, one set of models and one test suite.
- This app is mostly tables, forms and drill-downs. HTMX handles those with no front-end build step. Only the map needs real client-side JavaScript, and Cytoscape works without React.

| Part | Choice |
|---|---|
| Web | FastAPI, Jinja2, HTMX, Tailwind (prebuilt CSS, no Node in production) |
| DB | Postgres 16 on Railway, SQLAlchemy 2 + Alembic migrations |
| Jobs | Same codebase as a separate `worker` service. Postgres-backed job queue (`SELECT … FOR UPDATE SKIP LOCKED`). Railway cron only enqueues jobs. No Redis |
| Auth | Google OIDC (Authlib), checked against an allowlist of Dogstar Workspace domains (`hd` claim **and** email domain). Roles: admin, viewer |
| Research | Anthropic Python SDK (see §6) |
| Email digest | Workspace SMTP relay or Postmark (Q11) |
| Tests | pytest, run against a throwaway Postgres |

Railway services: `web`, `worker`, `cron` (enqueue only), `postgres` (backups on). Secrets only in Railway variables.

**Existing SQLite tool:** keep it working for now. The buyer profiles it builds from pingtree exports (price vs volume buyers, loan ranges, filters) are the input for "price/volume fit" in buyer scoring. In Phase 1 I'll port that into the new database as a `buyer_profile` input. The old CLI is retired once the new app covers it.

**Raw data:** following the repo's existing rule, nothing under `data/` goes into git. The spreadsheets live in `data/raw/` locally. On Railway they are uploaded through an admin "Imports" page, which stores the file with its SHA-256 and import time. Imports are idempotent per file hash.

---

## 3. Data model

Postgres, explicit tables. Everything that can have facts, edges or metrics shares one id space through a small `entity` table. This means claims, edges and metrics need one foreign key each, not a polymorphic column.

### 3.1 Entities

```
entity            id, entity_type (organisation|brand|domain|person|page), created_at
organisation      id→entity, legal_name, display_name, kind (operator_group|tribe|company|bank|agency|platform),
                  parent_id→organisation, hq_country, hq_state, notes,
                  lifecycle (candidate|active|screened_out|merged), merged_into_id
brand             id→entity, organisation_id, name
domain            id→entity, brand_id NULL, organisation_id, hostname UNIQUE, is_primary,
                  status (live|redirect|dead|unknown), redirect_target
person            id→entity, organisation_id, name, title, role_category, linkedin_url, email,
                  source, last_verified_at, lawful_basis_note, deleted_at
page              id→entity, domain_id, url UNIQUE, page_type (comparison_table|partner_list|rates|…)
name_alias        entity_id, alias, alias_norm, source_claim_id   -- every name seen, for matching
```

`lifecycle = screened_out` keeps the 185 screened-out CFPB companies in the database (so discovery doesn't "rediscover" them) but hides them by default.

### 3.2 Roles and footprint

```
org_role          organisation_id, role (lender_tribal|lender_state_licensed|lender_bank_partner|lender_cab_cso|
                  servicer|lead_network_pingtree|connector_brand|comparison_offer_wall|content_affiliate|
                  sponsored_publisher_agency|affiliate_cpa_network|paid_media_platform|fintech_decline_source),
                  tribe_id→organisation NULL, bank_partner_id→organisation NULL,
                  products text[], loan_min, loan_max, segment, online_or_branch, claim_ids int[]
entity_state      entity_id, state (2-letter), basis (licensed|lends|excluded), claim_ids int[]
```

### 3.3 Edges

```
relationship      id, from_id→entity, to_id→entity, type, status (confirmed|inferred|rumoured),
                  attrs jsonb (e.g. position, page_url for lists_offer_of), claim_ids int[],
                  first_seen, last_seen
```

Types as in the brief, plus `alias_of` for dedup decisions that aren't full merges.

### 3.4 Our relationships (authoritative, separate)

```
our_relationship  id, organisation_id, role (buyer|supplier|both|network),
                  status (live|paused|contracting|in_talks|former|prospect|parked|dropped),
                  owner, notes, source (internal_db|csv|manual), needs_confirmation bool,
                  external_ref (pingtree buyer id, for options 1 and 2), updated_by, updated_at
our_relationship_history   full copy of each change
```

The import path for the internal DB or CSV writes `external_ref` and `source`. Matching rows by `external_ref` keeps re-imports idempotent.

### 3.5 Claims (the core)

```
source            id, url, source_type (sec_filing|regulator|site_read|ahrefs|similarweb|cfpb|court|news|
                  job_post|spreadsheet_import|manual), retrieved_at, content_sha256, snapshot_ref,
                  import_file_id NULL, sheet, row_number    -- spreadsheet provenance down to the cell
claim             id, entity_id, attribute, value jsonb, source_id, method (job name + version, or user),
                  job_run_id NULL, confidence (high|medium|low),
                  status (proposed|accepted|rejected|superseded), superseded_by, created_at, decided_by
attribute_def     key, cardinality (one|set), value_schema, materiality rule jsonb, projects_to
entity_current    materialised view: latest accepted claim per (entity, attribute)
```

- A spreadsheet fact gets **two** sources when the row has URLs: the spreadsheet cell, plus each original URL from "Sources". Confidence comes from "Research confidence". If that is blank, it is `medium` for Targets and `low` for "Earlier research this chat".
- Structural tables (`brand`, `domain`, `org_role`, `entity_state`, `relationship`) are **projections** of accepted claims. Each row stores the claim ids behind it. That is how the UI reaches "why do we believe this?" in two clicks: field → claim list → source.
- Human edits in the UI are claims with `source_type = manual`. Nothing is overwritten in place.
- Change detection (Stage 2) is a diff of a new claim against `entity_current` for that attribute, judged by `attribute_def.materiality`.

### 3.6 Metrics, compliance, scores, review, audit, jobs

```
metric_snapshot   entity_id (org|domain|page), metric, value numeric, period, source_id, retrieved_at
tracked_keyword   keyword, state NULL, active
compliance_flag   entity_id, flag_type, summary, date, claim_id
compliance_status entity_id, value (clear|review|blocked), set_by, set_at, note  + history table
score             entity_id, score_type (buyer_value|supply_value|lbs_index|sheet_reference), value,
                  components jsonb (each with claim_ids), weights_version_id, computed_at   -- history kept
weights_version   id, score_type, weights jsonb, created_by, created_at, note
review_item       id, kind (possible_match|dedup|new_entrant|material_change|low_confidence|parse_failure|
                  relationship_confirm), entity_ids, payload jsonb, status (open|approved|rejected|edited),
                  decided_by, decided_at, note
audit_log         actor, action, table, row_id, before jsonb, after jsonb, at
job_run           job, args, status, started/ended, cost by source, claims_written, errors
api_usage         job_run_id, provider, units, cost_usd, at          -- budget enforcement reads this
http_cache        request_key, provider, url, status, body (compressed), fetched_at, expires_at
import_file       file_name, sha256, uploaded_by, imported_at, row_counts jsonb
```

The future outreach module only needs new tables (sequences, touches, suppression) that reference `person` and `our_relationship`. Nothing above has to change.

---

## 4. Import and dedup (Phase 1)

Order, all in one transaction per file, keyed on file hash so a re-run changes nothing:

1. **Owner-group seed** from both Read me sheets (Pay Day Ventures, Selling Source, PointClickTrack, Lead Stack Media, LendYou, Paradise Media, Fort Belknap / IMDG and the operator groups in Targets).
2. **Traffic file**: an organisation per row, a role from the category, a domain where present, and claims for every column (traffic as a `metric_snapshot` plus claim). Priority A/B/C is stored as the reference tier.
3. **Universe**: an organisation per CFPB name, complaint counts as metric snapshots, status as a claim, screened-out rows as `lifecycle = screened_out`.
4. **Targets**: claims for every column on the matching Universe org, plus parsed roles, brands, domains, states, tribe, parent/servicer edges and compliance flags from "Litigation / regulatory". The original score and its components are stored as `sheet_reference`.
5. **Lead-buying strength**: matched to existing orgs where possible, otherwise a new `candidate` org. The index and its components are stored, plus "lists it appears on" as `buys_leads_from` edges with status `inferred`.
6. **Dedup pass**, in the brief's order: domain → normalised legal name (strip LLC/Inc/dba, punctuation, case) → operator group. One exact domain or exact normalised name → auto-link. Anything weaker (fuzzy name, shared operator group only, alias list overlap) → a `dedup` review item, **not** merged.
7. **Seed `our_relationships`** with the 9 names in §2.3 of the brief, all `needs_confirmation = true`, each with known domains and aliases (I'll fill these in and you confirm them).
8. **Possible-match sweep** over the whole universe against every `our_relationship` org: shared domain, brand, alias, operator group or tribe → `possible_match` review item. The 23 + 54 spreadsheet "existing client" labels feed the same queue.

**Phase 1 tests:** missing domains (11 rows kept and grouped), GVA ↔ Cash Advance Now caught, Pay Day Ventures has 5 brands under one owner, idempotent re-import, and count reconciliation per sheet and status.

---

## 5. Scoring

Three separate measures, as in the brief. Never one combined number.

- **Buyer value (lenders):** lead-buying strength (port the existing 0–100 method, components and all), size (originations if disclosed, otherwise log complaint volume plus traffic and paid spend), price/volume fit (loan range against our price and volume buyer profiles from the pingtree export), type fit, state overlap with our traffic states (neutral until the internal data arrives), segment.
- **Supply value:** traffic volume (page-level for tables), subprime-loan relevance, overlap with our current traffic, ease (self-serve vs relationship deal), owner-group leverage (number of brands one contact unlocks).
- **Compliance gate:** `blocked` hidden from ranked views, `review` shown with a warning. Not part of either score.

Every component stores the claim ids it used. Weights are versioned. "Re-score" in Admin writes a new `score` row per entity under the new version and logs who ran it. The original spreadsheet score sits next to the new one, with the rank change shown.

---

## 6. Research engine (Stage 2, also used in Phase 4)

I checked the current Anthropic models and tools:

| Use | Model | Why |
|---|---|---|
| Bulk extraction from pages we have already fetched (partner lists, rates pages, state lists, privacy policies) | `claude-haiku-5-5` ($0.10 / $0.50 per M tokens), via the Batches API (50% off) for large passes | The cheap tier the brief asks for. No web search needed, because our own fetcher gets the page |
| Per-entity research with web search (lightweight pass, candidate research) | `claude-sonnet-5-5` ($2 / $10) with `web_search_20260209` and `web_fetch_20260209` | Good balance for 450+ entities |
| Owner-group reasoning, synthesis, change summaries for the digest | `claude-opus-5-5` ($4 / $20) | Strongest tier |

The model for each job is a setting in Admin, not a constant in code.

Rules built into the runner:
- **Structured output** (`output_config.format` with a JSON schema): every claim must include `source_url`, plus a supporting quote where possible. The runner **rejects any claim whose URL did not appear in that job's search or fetch results**, so made-up citations never reach the database.
- **Prompt injection:** page text goes in as data inside tool results. The system prompt says fetched content is never instructions. Outputs are schema-validated, and agents have no tools that write to the database. Only the runner writes, and only proposed claims.
- **Our own fetcher** (ported from `fetch.py`): honest User-Agent, robots.txt, per-domain rate limit, responses cached in `http_cache`. NMLS Consumer Access and any site whose terms forbid automation → a manual-check review item, not a fetch.
- **No LinkedIn automation of any kind.** Contacts come from role-based search lists that the team works by hand in Sales Navigator, from public sources (10-K, press releases, OLA board, speaker pages, careers pages) and from licensed enrichment once you approve it.
- **Budgets:** each job checks `api_usage` against the monthly cap per provider (Anthropic, Ahrefs, Similarweb) before each call. At the cap it stops, marks the run `budget_stopped` and alerts admins.

**Rough cost, to be confirmed in a pilot:** a lightweight pass on Sonnet with about 5 searches is roughly $0.10–0.30 per entity, so the 453 unresearched CFPB candidates come to roughly $50–150 in total. Haiku extraction is under a cent per page. I'll run a 20-entity pilot and report the real per-entity cost before the full run.

---

## 7. Jobs (Stage 2)

| Job | Cadence | Writes |
|---|---|---|
| `refresh_entity` | A tier and live: weekly; B: monthly; C: quarterly | metrics, key-page re-reads, new claims |
| `compliance_watch` | weekly, all non-blocked | CFPB/FTC actions, state alerts (e.g. WA DFI), court and news hits |
| `discover_cfpb` | weekly | new company names with relevant complaints since the last run |
| `discover_serp` | weekly | new domains in the top results for tracked keywords, plus page-level table sizing |
| `discover_partner_lists` | with refresh | names on known lenders' and networks' consent lists that aren't in the database (strongest new-buyer signal) |
| `discover_affiliate_dirs`, `discover_hiring`, `discover_fingerprints` | monthly | candidates |
| `diff_and_flag` | after each job | material change → review item; immaterial → accept quietly |
| `digest` | weekly (day TBC) | email: new candidates, material changes, compliance events, top-50 score moves, all linking into the app |

A discovered candidate gets one lightweight research pass, then lands in the inbox as `new_entrant` with a suggested type, owner group and score. **Nothing is promoted and no relationship is created without a human approving it.** Each job run logs its inputs, cost, claims written and errors.

---

## 8. UI (Phases 2–3)

As in brief §8. Two choices worth stating:
- **Home** has three blocks only: top unworked buyers and supply sources, recent material changes, and the review count by kind.
- **Evidence drawer:** every field on an entity page has a small "i". One click opens the claims for that field (value, source, date, confidence, method). A second click opens the source (URL, or file / sheet / row for spreadsheet facts). That meets the two-click acceptance test.
- **Map default view:** A/B priority plus everything one hop from live relationships. Owner groups are collapsed into compound nodes and expand on click.

---

## 9. Security (SOC 2 scope)

- Google SSO with a domain allowlist. Every route needs a session except `/healthz`.
- Railway gives the web service a public hostname. With SSO only on every route, nothing is readable without a Dogstar login. If you want network-level restriction as well, Cloudflare Access in front is cheap to add (Q10).
- Audit log on every write, including reviews, weight changes and relationship edits.
- Backups on for Postgres. No borrower PII and no pingtree lead data ever enter this database. The internal relationship import takes buyer or supplier identity and status only.
- People records: business contact data only, a lawful-basis note on each, and a delete-on-request action that hard-deletes and logs the deletion.

---

## 10. Phases (unchanged from the brief, with what I'll show at each gate)

| Phase | Deliverable | Gate evidence |
|---|---|---|
| 1 Import & model | Schema, importers, seeds, dedup queue, possible-match sweep, tests | Reconciliation report per sheet; GVA and Pay Day Ventures tests passing |
| 2 Core UI | Home, tables + CSV, entity page + evidence drawer, review inbox, admin weights, SSO | Walkthrough: any field → source in two clicks |
| 3 Map & states | Layered Cytoscape map, filters, owner-group collapse; state map | Screenshots and default-view node count |
| 4 Enrichment | 20-entity pilot → 453 lightweight passes; Ahrefs gaps; keyword table sizing; contact search lists for A/B | Every A/B entity has a size estimate with method, a compliance status and a search list |
| 5 Automation | Jobs, diffing, digest, budgets | Two-week dry run: digest samples, spend against budget, zero unreviewed promotions |

---

## 11. Questions

From the brief:
1. **Authoritative buyer/supplier list:** can James provide a read-only view or a regular CSV export from the pingtree DB? Until then I'll build the manual table plus CSV import.
2. **Ahrefs and Similarweb:** which API plans, and what monthly unit budgets can this app use?
3. **Anthropic:** monthly budget ceiling for research runs? I suggest $300/month to start, with the pilot reported first.
4. **Reviewers:** who reviews the inbox and approves promotions (Layton, Chris, the new US BDM)? Same people for compliance status?
5. **Keyword set:** confirm the starter list. My proposal: "bad credit loans", "payday loans online", "installment loans for bad credit", "personal loans for bad credit", "cash advance online", "loans for bad credit guaranteed approval", plus "payday loans [state]" and "installment loans [state]" for the top 10 traffic states.
6. **Contact enrichment:** is Apollo or Clay already licensed, or Similarweb only for now?
7. **Digest:** who gets it, and which day?

New, from reading the data:

8. **Blue Oak Credit:** the brief says *in talks*, but the targets file treats Blue Oak Credit (and Kashia Services, Kashia Band of Pomo) as an *existing client*. Which is right? I'll seed it as `in_talks`, needs confirmation, until you say otherwise.
9. **The 54 "existing client" names in the Lead-buying strength sheet** (CreditFresh, Credit Ninja, MoneyKey, Cane Bay and others): my plan is to put them in the review queue as possible matches, not seed them as live. Is that right, or do you want them seeded as `live`, needs confirmation, since they came from the pingtree buyer list?
10. **Hosting exposure:** is SSO on every route enough, or do you want Cloudflare Access (or an IP allowlist) in front of the Railway URL as well?
11. **SSO domains and email:** which Google Workspace domains may sign in, and can the digest send through Workspace SMTP, or should I use Postmark?
12. **Stack:** OK with Python (FastAPI + HTMX) instead of Next.js? Reason: it reuses the existing Python research code and keeps the app and workers in one codebase.
13. **Seed domains:** I'll attach the known domains to the 9 seed relationships (e.g. cashadvancenow.com, zeroparallel.com, itmedia.com). Please confirm or correct the list when I show it in Phase 1.
