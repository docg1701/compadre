-- F19 — tighten ``api_key.rate_limit_rpm`` CHECK to ``BETWEEN 1 AND 10000``.
-- The CLI already caps the value at 10 000 in ``issue-key`` / ``update-key``,
-- but a direct SQL edit or a malformed backup restore could plant a value
-- outside that range. SQLite has no ``ALTER COLUMN ADD CHECK``, so the
-- migration follows the canonical create-new-table + copy + rename dance.
--
-- Foreign keys: ``api_key`` is referenced by no other table, so PRAGMA
-- foreign_keys defer / re-enable boilerplate is unnecessary.
--
-- Indexes are recreated AFTER the data copy because they are dropped along
-- with the original table; the names below MUST byte-match v001's
-- definitions so downstream EXPLAIN-QUERY-PLAN tooling does not regress.

CREATE TABLE api_key_v2 (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customer(id) ON DELETE RESTRICT,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    rate_limit_rpm  INTEGER NOT NULL CHECK (rate_limit_rpm BETWEEN 1 AND 10000),
    status          TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
    billing_mode    TEXT NOT NULL CHECK (billing_mode IN ('manual', 'complimentary', 'automated')),
    price_monthly   REAL NOT NULL DEFAULT 0.0,
    paid_until      TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    notes           TEXT DEFAULT ''
);

INSERT INTO api_key_v2 (
    id, customer_id, key_hash, key_prefix, rate_limit_rpm,
    status, billing_mode, price_monthly, paid_until,
    created_at, last_used_at, notes
)
SELECT
    id, customer_id, key_hash, key_prefix, rate_limit_rpm,
    status, billing_mode, price_monthly, paid_until,
    created_at, last_used_at, notes
FROM api_key;

DROP TABLE api_key;
ALTER TABLE api_key_v2 RENAME TO api_key;

CREATE UNIQUE INDEX idx_api_key_hash ON api_key(key_hash);
CREATE INDEX idx_api_key_customer ON api_key(customer_id);
CREATE INDEX idx_api_key_sweep ON api_key(billing_mode, paid_until) WHERE status = 'active';
