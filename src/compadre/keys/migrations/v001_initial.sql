CREATE TABLE customer (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    tax_id_kind     TEXT NOT NULL CHECK (tax_id_kind IN ('cpf', 'cnpj')),
    tax_id_encrypted BLOB NOT NULL,
    notes           TEXT DEFAULT '',
    registered_at   TEXT NOT NULL,
    deleted_at      TEXT
);

CREATE TABLE api_key (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customer(id) ON DELETE RESTRICT,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    rate_limit_rpm  INTEGER NOT NULL CHECK (rate_limit_rpm > 0),
    status          TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
    billing_mode    TEXT NOT NULL CHECK (billing_mode IN ('manual', 'complimentary', 'automated')),
    price_monthly   REAL NOT NULL DEFAULT 0.0,
    paid_until      TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    notes           TEXT DEFAULT ''
);

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at     TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    details_json    TEXT
);

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_api_key_hash ON api_key(key_hash);
CREATE INDEX idx_api_key_customer ON api_key(customer_id);
CREATE INDEX idx_api_key_sweep ON api_key(billing_mode, paid_until) WHERE status = 'active';
CREATE INDEX idx_audit_occurred ON audit_log(occurred_at);
