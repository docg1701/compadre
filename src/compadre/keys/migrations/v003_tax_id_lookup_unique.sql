-- B2 — Enforce CPF/CNPJ uniqueness via deterministic HMAC lookup column.
--
-- Threat model: an operator who runs ``create-customer`` twice with the
-- same CPF should be rejected at the database boundary, not silently allowed
-- to produce two customer rows with overlapping tax-IDs (LGPD Art. 18 II
-- minimum-necessary; forensic identity-anchor invariant).
--
-- Direct UNIQUE on ``tax_id_encrypted`` is impossible: Fernet randomises
-- the IV on every encryption, so the same plaintext yields different
-- ciphertexts. We therefore add a separate column ``tax_id_lookup`` that
-- stores HMAC-SHA256(KEY_REGISTRY_ENCRYPTION_KEY, normalized_tax_id) — a
-- deterministic 32-byte digest with the same secret as the ciphertext.
-- An attacker who steals the DB file alone cannot brute-force the lookup
-- without the encryption key (same defense-in-depth as ``tax_id_encrypted``).
--
-- The UNIQUE PARTIAL INDEX excludes:
-- - tombstoned rows (``deleted_at IS NOT NULL``) — re-registering a CPF
--   after LGPD hard-delete is allowed; the tombstone holds no PII.
-- - NULL lookups — existing pre-v3 rows that haven't been backfilled yet.
--   The ``backfill_tax_id_lookup`` helper (called from FastAPI lifespan
--   and ``compadre-keys`` main on startup) populates them; until then,
--   uniqueness is enforced only against new rows. The narrow window is
--   acceptable for operator-CLI traffic — the alternative (mandatory
--   migration-time backfill) would couple migrations to settings/Fernet
--   and break all the test fixtures that call ``_apply_migrations(conn)``
--   without settings context.

ALTER TABLE customer ADD COLUMN tax_id_lookup BLOB;

CREATE UNIQUE INDEX idx_customer_tax_id_lookup
    ON customer(tax_id_lookup)
    WHERE deleted_at IS NULL AND tax_id_lookup IS NOT NULL;
