-- Prospecting knowledge base (SQLite).
-- Raw imports are kept as loaded; buyer-level views are rebuilt from them.

CREATE TABLE IF NOT EXISTS import_batch (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,          -- 'revenue' | 'filters'
    file_name   TEXT NOT NULL,
    period      TEXT,                   -- e.g. 'last 30 days to 2026-09-25'
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per buyer company after merging aliases.
CREATE TABLE IF NOT EXISTS buyer (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    category       TEXT,                -- D direct lender | S service provider | N network | O non-lender offer
    status         TEXT,                -- user-set: active | paused by us | paused by them | testing | lost
    website        TEXT,
    notes          TEXT
);

-- Every name a buyer appears under in pingtree exports.
CREATE TABLE IF NOT EXISTS buyer_alias (
    alias    TEXT PRIMARY KEY,
    buyer_id INTEGER NOT NULL REFERENCES buyer(id)
);

CREATE TABLE IF NOT EXISTS tier_revenue (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES import_batch(id),
    raw_name        TEXT NOT NULL,
    alias           TEXT NOT NULL,
    tier_name       TEXT NOT NULL,
    is_price_reject INTEGER NOT NULL,
    processed       INTEGER, sent INTEGER, unique_sold INTEGER, multi_sell INTEGER,
    total_sold      INTEGER, declined INTEGER, error INTEGER, redirected INTEGER,
    commission      REAL,
    epl             REAL,
    response_secs   REAL
);

CREATE TABLE IF NOT EXISTS tier_filter (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES import_batch(id),
    alias         TEXT NOT NULL,
    tier_name     TEXT,                 -- NULL when the export put a filter value in the tier column
    raw_tier      TEXT NOT NULL,
    filter_type   TEXT NOT NULL,
    filter_value  TEXT NOT NULL
);

-- Prospect companies and contacts (populated from step 2 onwards).
-- Every record carries its source; contacts also carry a lawful basis (UK GDPR).
CREATE TABLE IF NOT EXISTS company (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    website       TEXT,
    category      TEXT,
    buyer_id      INTEGER REFERENCES buyer(id),   -- set when the company is an existing buyer
    source        TEXT NOT NULL,
    source_url    TEXT,
    retrieved_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_fact (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES company(id),
    field        TEXT NOT NULL,         -- e.g. 'states', 'products', 'licence'
    value        TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    quote        TEXT,                  -- supporting text from the source
    verified_by  TEXT,                  -- NULL until a person checks it
    retrieved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contact (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES company(id),
    full_name     TEXT NOT NULL,
    title         TEXT,
    email         TEXT,
    linkedin_url  TEXT,
    source        TEXT NOT NULL,        -- e.g. 'Sales Navigator (manual)'
    lawful_basis  TEXT NOT NULL DEFAULT 'legitimate interests (B2B prospecting)',
    collected_by  TEXT,
    collected_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suppression (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,            -- 'email' | 'domain' | 'company'
    value     TEXT NOT NULL UNIQUE,
    reason    TEXT,
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
