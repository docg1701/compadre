"""SQLite connection management and CRUD for the key registry.

All queries use parameterized `?` placeholders — never f-strings or
`.format()`. WAL journal mode and `foreign_keys=ON` are enabled at connect
time. File permissions (0600) and parent-directory permissions (0700) are
enforced on creation.

Migration sentinel: a `schema_version` table records the highest applied
version. The runner checks `sqlite_master` first (no `CREATE TABLE IF NOT
EXISTS` in the v001 SQL — that SQL runs only on fresh DBs).
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import sqlite3
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from ulid import ULID

from compadre.keys.models import (
    ApiKeyListRow,
    ApiKeyRecord,
    AuditEventRow,
    AuthPayload,
    BillingMode,
    CustomerDetailRow,
    CustomerListRow,
    CustomerRecord,
    CustomerStatus,
    KeyDetailRow,
    TaxIdKind,
)

_logger = logging.getLogger(__name__)

ShowTargetKind = Literal["customer", "key_single", "key_ambiguous", "not_found"]
UpdatedField = Literal["email", "notes", "name"]
UpdateKeyField = Literal["notes", "price", "rpm"]


@dataclass(frozen=True, slots=True)
class CustomerExportRow:
    """Active-customer export payload — includes the encrypted tax-ID blob.

    Surfaced exclusively for ``export-customer`` (Story 4.1); every other
    read path goes through ``get_customer_detail`` which deliberately
    omits the ciphertext. Tombstoned customers are treated as missing
    (``deleted_at IS NULL`` filter), matching ``get_customer`` /
    ``get_customer_detail``.
    """

    id: str
    name: str
    email: str
    tax_id_kind: TaxIdKind
    tax_id_encrypted: bytes
    notes: str
    registered_at: str


@dataclass(frozen=True, slots=True)
class DeleteCustomerResult:
    """Return payload from ``delete_customer_hard`` (Story 4.1).

    ``keys_purged`` is the count of ``api_key`` rows DELETEd in the same
    transaction as the customer-tombstone UPDATE (and one ``delete_customer``
    audit row). ``customer_name`` is captured PRE-purge so the ConfirmationLine
    can render ``Deleted: cust_... (Marcos Silva) — 2 keys purged`` —
    post-purge the name column reads empty string.
    """

    customer_id: str
    customer_name: str
    keys_purged: int


@dataclass(frozen=True, slots=True)
class RotateEncryptionKeysResult:
    """Return payload from ``rotate_encryption_keys`` (Story 4.2).

    ``rows_re_encrypted`` is the count of ``customer`` rows whose
    ``tax_id_encrypted`` was decrypted under the old key and rewritten
    under the new key in the same transaction. Tombstoned rows
    (``deleted_at IS NOT NULL`` — zero-length BLOB ciphertext per the
    Story 4.1 tombstone contract) are intentionally skipped; the count
    reflects active customers only. ``occurred_at`` is the canonical
    ISO-UTC timestamp the helper wrote on the summary audit row, echoed
    back so the CLI can render it without re-sampling the clock.
    """

    rows_re_encrypted: int
    occurred_at: str


@dataclass(frozen=True, slots=True)
class UpdateCustomerResult:
    """Return payload from ``update_customer``.

    Carries the refreshed ``CustomerDetailRow`` so the CLI can feed it to
    the ConfirmationLine renderer without a follow-up ``get_customer_detail``
    round-trip. ``updated_fields`` is a sorted, de-duplicated tuple of the
    columns the caller actually touched — used for both the audit payload
    and the human suffix.
    """

    customer: CustomerDetailRow
    updated_fields: tuple[UpdatedField, ...]
    notes_op: Literal["append", "replace"] | None = None


@dataclass(frozen=True, slots=True)
class KeyFieldDeltas:
    """Old/new snapshot of the numeric fields ``update_api_key`` touched.

    ``None`` on either side means the field was not in the update set; the
    CLI omits the clause from the ConfirmationLine. Carrying both old and
    new lets the renderer assemble the ``60→120`` arrow form without a
    second DB read.
    """

    rpm_old: int | None
    rpm_new: int | None
    price_old: float | None
    price_new: float | None


@dataclass(frozen=True, slots=True)
class UpdateKeyResult:
    """Return payload from ``update_api_key``.

    ``changes`` captures the pre-/post-mutation numeric values so the
    ConfirmationLine can render the arrow form (``rpm: 60→120``). ``notes``
    replacement does not populate ``changes`` — the renderer emits the
    fixed ``notes updated`` clause instead.
    """

    key: KeyDetailRow
    updated_fields: tuple[UpdateKeyField, ...]
    changes: KeyFieldDeltas


@dataclass(frozen=True, slots=True)
class RotateKeyResult:
    """Return payload from ``rotate_api_key`` — carries the fresh record.

    The ``record`` field carries the in-memory ``ApiKeyRecord`` assembled
    from the pre-UPDATE SELECT + the freshly-computed hash / prefix — the
    helper skips a second DB read because every field of the refreshed
    row is already known inside the transaction. ``old_prefix`` feeds the
    audit-tail delta and the structured log line.
    """

    record: ApiKeyRecord
    customer_name: str
    old_prefix: str


@dataclass(frozen=True, slots=True)
class MarkPaidResult:
    """Return payload from ``mark_key_paid``.

    ``key`` is the refreshed ``KeyDetailRow`` (post-mutation). ``reactivated``
    captures the FR20 side-effect: True when the row was ``suspended`` and
    this call flipped it to ``active``; False when the row was already
    ``active`` and only ``paid_until`` changed. The CLI uses the flag to
    render a slightly different ConfirmationLine suffix (``... → paid until
    2026-06-05 (reactivated)`` vs just ``... → paid until 2026-06-05``).
    """

    key: KeyDetailRow
    reactivated: bool


@dataclass(frozen=True, slots=True)
class SuspendKeyResult:
    """Return payload from ``suspend_key`` — refreshed ``KeyDetailRow`` only."""

    key: KeyDetailRow


@dataclass(frozen=True, slots=True)
class SuspensionSweepResult:
    """Return payload from ``run_suspension_sweep``.

    ``swept_key_ids`` is the full list of ``api_key.id`` values that flipped
    from ``active`` to ``suspended``. ``swept_count`` is ``len(swept_key_ids)``
    exposed separately so the CLI can log a count without materializing the
    tuple. ``completed_at`` is the canonical ISO-UTC timestamp
    (``%Y-%m-%dT%H:%M:%SZ``) the sweep wrote on its summary audit row —
    returned so the CLI's ConfirmationLine shows the same timestamp that the
    ``health`` command (Story 3.4) will later read from the audit log.
    ``cutoff_date`` is the ``today`` the caller passed, echoed back so the
    CLI can render it verbatim without re-parsing.
    """

    swept_key_ids: tuple[str, ...]
    swept_count: int
    completed_at: str
    cutoff_date: str


@dataclass(frozen=True, slots=True)
class ReactivateKeyResult:
    """Return payload from ``reactivate_key`` — refreshed ``KeyDetailRow`` only."""

    key: KeyDetailRow


@dataclass(frozen=True, slots=True)
class RevokeKeyResult:
    """Return payload from ``revoke_api_key``.

    The refreshed ``KeyDetailRow`` carries the new ``status='revoked'``
    state so the CLI can render the ConfirmationLine + any follow-up
    ``show``-style surface without a second round-trip. ``reason`` is
    echoed back on the operator's console (``FR37`` covers logs/audit,
    not terminal display).
    """

    key: KeyDetailRow
    customer_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ShowTarget:
    """Discriminated result of ``resolve_show_target``.

    ``kind`` is the branch tag; ``value`` carries the matching payload. A
    caller does ``if result.kind == "customer": ...`` instead of sniffing
    string shapes or payload types.
    """

    kind: ShowTargetKind
    value: CustomerDetailRow | KeyDetailRow | list[KeyDetailRow] | None


_SCHEMA_VERSION = 3


def _utcnow_iso() -> str:
    """Return current UTC time as ISO 8601 seconds (Z suffix, no microseconds)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_customer_id() -> str:
    """Return a fresh `cust_<26-char-lowercase-ULID>` identifier."""
    return f"cust_{str(ULID()).lower()}"


def generate_key_id() -> str:
    """Return a fresh `key_<26-char-lowercase-ULID>` identifier."""
    return f"key_{str(ULID()).lower()}"


def _chmod_0600(target: Path) -> None:
    """Set mode 0600 on `target` if it exists and differs."""
    if not target.exists():
        return
    if stat.S_IMODE(target.stat().st_mode) != 0o600:
        target.chmod(0o600)


def _touch_auth_cache_sentinel(conn: sqlite3.Connection) -> None:
    """Advance the sentinel mtime so API processes flush their auth cache.

    The sentinel lives next to the DB file at ``{db_path}.invalidate``.
    ``AuthCache.get()`` stats it on every read and flushes when mtime
    advances — this gives synchronous cross-process invalidation after
    every ``api_key`` mutation (update / rotate / revoke). Called outside
    the SQLite transaction so it only runs on successful commit.

    ``pragma_database_list`` returns the file path the main database is
    attached to; an in-memory DB (``:memory:`` / blank file) returns an
    empty string and we silently skip — no sentinel to maintain.

    The FS touch is best-effort: a post-commit ``OSError`` (disk full,
    EPERM, read-only FS) is swallowed rather than re-raised. The DB commit
    is authoritative; cache staleness falls back to ``AuthCache``'s 60 s
    TTL in that window.
    """
    row = conn.execute(
        "SELECT file FROM pragma_database_list WHERE name = 'main'"
    ).fetchone()
    if row is None or not row[0]:
        return
    sentinel = Path(row[0] + ".invalidate")
    try:
        # F17 — write a monotonic counter (``time.time_ns()``) into the
        # file content. The atomic tmp-file + rename pattern means a
        # concurrent reader sees either the old value or the new one,
        # never a partial write. Compared to the prior touch-only design,
        # this sidesteps coarse-mtime same-second collisions on FAT32 /
        # overlayfs — see ``AuthCache._read_sentinel_token`` for the
        # reader contract.
        #
        # ``time.time_ns()`` (wall clock) — not ``monotonic_ns()`` —
        # because the reader's mtime fallback is also wall clock; mixing
        # domains would let a content-int (small) lose a comparison
        # against a fallback mtime (large epoch ns). Rollback regression
        # is mitigated below by reading the current content and bumping
        # if the new value is not strictly greater.
        import time

        new_token = time.time_ns()
        # Read current content first so a clock-rollback (e.g. NTP step
        # back) cannot regress the counter. Reader uses ``>``, so a
        # regressed value would silently disable invalidation.
        try:
            current_text = sentinel.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            current_text = ""
        try:
            current = int(current_text) if current_text else 0
        except ValueError:
            current = 0
        if new_token <= current:
            new_token = current + 1
        tmp = sentinel.with_name(sentinel.name + ".tmp")
        tmp.write_text(str(new_token), encoding="utf-8")
        tmp.replace(sentinel)
        _chmod_0600(sentinel)
    except OSError as exc:
        # The DB commit is authoritative; cache staleness falls back to
        # AuthCache's 60s TTL in this window. WARN so operators with disk-
        # full / EPERM / read-only-FS scenarios see why cross-process
        # invalidation has degraded to TTL-only.
        from compadre.logging import get_logger

        get_logger("keys.db").warning(
            "auth-cache sentinel touch failed (%s: %s); cross-process "
            "invalidation degraded to AuthCache 60s TTL until resolved",
            type(exc).__name__,
            exc,
        )


def get_connection(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + foreign_keys enforced.

    - Parent directory is created with mode 0700 (operator-only).
    - File is chmod'd to 0600 once SQLite has created it. WAL sidecars
      (`*.db-wal`, `*.db-shm`) are chmod'd 0600 as well — they contain the
      same unflushed ciphertext and audit data as the main file.
    - `busy_timeout=5000` tolerates concurrent CLI invocations holding the
      write lock instead of failing immediately with SQLITE_BUSY.
    - `row_factory = sqlite3.Row` so callers can use name-based access.
    """
    from compadre.logging import get_logger

    db_path = Path(path)
    parent = db_path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        # Best-effort tightening on an existing parent that may have been
        # created with broader defaults. Ignored on bind mounts / shared dirs.
        try:
            parent.chmod(0o700)
        except PermissionError:
            get_logger("keys.db").warning(
                "could not chmod 0700 on registry parent dir %s "
                "(insufficient privileges); the file itself is still 0600",
                parent,
            )

    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        _chmod_0600(db_path)
        _chmod_0600(db_path.with_name(db_path.name + "-wal"))
        _chmod_0600(db_path.with_name(db_path.name + "-shm"))
    except Exception:
        conn.close()
        raise
    return conn


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations. Idempotent. Public alias of `_apply_migrations`."""
    _apply_migrations(conn)


_MIGRATION_FILES: tuple[tuple[int, str], ...] = (
    (1, "v001_initial.sql"),
    (2, "v002_rate_limit_upper_bound.sql"),
    (3, "v003_tax_id_lookup_unique.sql"),
)


def backfill_tax_id_lookup_if_needed(
    conn: sqlite3.Connection, *, encryption_key: SecretStr
) -> int:
    """Populate ``customer.tax_id_lookup`` for any active rows where it is NULL.

    Idempotent. Returns the number of rows backfilled. Called from FastAPI
    lifespan and ``compadre-keys`` startup so v2→v3 upgrades become invisible
    to the operator: the partial UNIQUE INDEX takes effect on the next
    ``create-customer`` after the first backfill run.

    Tombstoned rows (``deleted_at IS NOT NULL``) are skipped — their
    ciphertext is the zero-length blob ``X''`` and Fernet would raise
    ``InvalidToken``; LGPD scrubbed any PII so the lookup column has
    nothing meaningful to record.

    The single-Fernet invariant is preserved: this helper imports
    ``decrypt_tax_id`` and ``compute_tax_id_lookup`` from
    ``keys.crypto``; no other module gets a Fernet handle.
    """
    from compadre.keys.crypto import compute_tax_id_lookup, decrypt_tax_id

    rows = conn.execute(
        "SELECT id, tax_id_encrypted FROM customer "
        "WHERE deleted_at IS NULL AND tax_id_lookup IS NULL"
    ).fetchall()
    if not rows:
        return 0
    backfilled = 0
    with conn:
        for row in rows:
            canonical = decrypt_tax_id(bytes(row["tax_id_encrypted"]), encryption_key)
            lookup = compute_tax_id_lookup(canonical, encryption_key)
            cursor = conn.execute(
                "UPDATE customer SET tax_id_lookup = ? "
                "WHERE id = ? AND deleted_at IS NULL AND tax_id_lookup IS NULL",
                (lookup, row["id"]),
            )
            backfilled += cursor.rowcount
    return backfilled


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations. Idempotent.

    Each migration file in ``_MIGRATION_FILES`` runs in its own transaction
    along with the matching ``INSERT INTO schema_version`` row so a
    mid-migration crash leaves the registry at the LAST successful version
    rather than halfway between.
    """
    cursor = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_version'"
    )
    has_version_table = cursor.fetchone() is not None
    current: int = 0
    if has_version_table:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        # SELECT MAX on empty table returns (None,)
        current = int(row[0]) if row and row[0] is not None else 0

    if current > _SCHEMA_VERSION:
        raise RuntimeError(
            f"Key registry schema version {current} is newer than this binary "
            f"supports (max {_SCHEMA_VERSION}). Upgrade compadre before running."
        )
    if current == _SCHEMA_VERSION:
        return

    for version, filename in _MIGRATION_FILES:
        if version <= current:
            continue
        sql = (
            importlib.resources.files("compadre.keys.migrations")
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )
        with conn:
            # executescript handles multi-statement DDL safely, including
            # statements whose bodies contain semicolons (triggers, CHECK
            # clauses with strings).
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _utcnow_iso()),
            )


def insert_customer(
    conn: sqlite3.Connection,
    record: CustomerRecord,
    *,
    actor: str,
    tax_id_lookup: bytes | None = None,
) -> None:
    """Insert a customer and its audit row in a single transaction.

    Callers must NOT open a surrounding `with conn:` — this function opens
    the transaction itself so the customer and audit writes either both
    commit or both roll back.

    ``tax_id_lookup`` (B2): deterministic HMAC of the canonical tax-ID
    keyed by ``KEY_REGISTRY_ENCRYPTION_KEY`` — feeds the v3 partial UNIQUE
    INDEX. Callers compute it via ``crypto.compute_tax_id_lookup``. ``None``
    is accepted for backwards compatibility (test fixtures that don't care
    about the duplicate-detection invariant); production write paths
    (``create-customer``, ``correct-tax-id``) MUST pass a value so the
    UNIQUE constraint applies.

    ``sqlite3.IntegrityError`` from the partial UNIQUE INDEX surfaces to
    the caller, which translates it to the user-facing "tax-ID already
    registered" message.
    """
    from compadre.keys.audit import log_event

    registered_at_iso = record.registered_at.astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    deleted_at_iso = (
        record.deleted_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if record.deleted_at is not None
        else None
    )
    with conn:
        conn.execute(
            "INSERT INTO customer ("
            "id, name, email, tax_id_kind, tax_id_encrypted, tax_id_lookup, "
            "notes, registered_at, deleted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.name,
                record.email,
                record.tax_id_kind,
                record.tax_id_encrypted,
                tax_id_lookup,
                record.notes,
                registered_at_iso,
                deleted_at_iso,
            ),
        )
        log_event(
            conn,
            actor=actor,
            action="create_customer",
            target_type="customer",
            target_id=record.id,
            details={"tax_id_kind": record.tax_id_kind},
        )


def get_customer(
    conn: sqlite3.Connection,
    customer_id: str,
) -> CustomerRecord | None:
    """Return the customer row matching `customer_id`, or None.

    Tombstoned customers (`deleted_at IS NOT NULL`) are treated as missing
    — this matches Story 1.2 AC3 (LGPD tombstone is opaque to the CLI).
    """
    row = conn.execute(
        "SELECT id, name, email, tax_id_kind, tax_id_encrypted, notes, "
        "registered_at, deleted_at FROM customer "
        "WHERE id = ? AND deleted_at IS NULL",
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    return CustomerRecord(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        tax_id_kind=row["tax_id_kind"],
        tax_id_encrypted=bytes(row["tax_id_encrypted"]),
        notes=row["notes"] or "",
        registered_at=datetime.fromisoformat(
            row["registered_at"].replace("Z", "+00:00")
        ),
        deleted_at=None,
    )


def get_customer_with_cipher(
    conn: sqlite3.Connection,
    customer_id: str,
) -> CustomerExportRow | None:
    """Return the export payload for ``customer_id``, or ``None`` if missing.

    Tombstoned customers (``deleted_at IS NOT NULL``) are treated as
    missing. Used only by ``export-customer`` (Story 4.1) — the
    ciphertext BLOB must NEVER be returned by a general-purpose helper
    that callers might repurpose for logs / panels / caches.
    """
    row = conn.execute(
        "SELECT id, name, email, tax_id_kind, tax_id_encrypted, notes, "
        "registered_at FROM customer "
        "WHERE id = ? AND deleted_at IS NULL",
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    return CustomerExportRow(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        tax_id_kind=cast(TaxIdKind, row["tax_id_kind"]),
        tax_id_encrypted=bytes(row["tax_id_encrypted"]),
        notes=row["notes"] or "",
        registered_at=row["registered_at"],
    )


def update_customer(
    conn: sqlite3.Connection,
    customer_id: str,
    *,
    new_email: str | None,
    add_note: str | None,
    new_name: str | None = None,
    replace_notes: str | None = None,
    actor: str,
    today: date,
) -> UpdateCustomerResult | None:
    """Apply the email/name/notes mutation inside a single transaction.

    Returns ``None`` when the customer is missing or tombstoned. Otherwise
    commits the mutation + an ``update_customer`` audit row and returns the
    refreshed detail row.

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE and the audit write either both commit or
    both roll back (same contract as ``insert_customer`` / ``insert_api_key``).

    ``add_note`` (append-mode) and ``replace_notes`` (replace-mode) are
    mutually exclusive; passing both raises ``ValueError`` (the CLI layer
    rejects pre-call, this is defense-in-depth for future programmatic
    callers).

    ``today`` is the operator-TZ date used to stamp the appended note
    (``[YYYY-MM-DD] text``). The caller resolves it via the existing
    ``cli._clock.resolve_today_in_tz`` seam so tests freeze the clock with
    ``monkeypatch`` instead of the helper touching ``date.today()`` directly.
    """
    from compadre.keys.audit import log_event

    if add_note is not None and replace_notes is not None:
        raise ValueError("add_note and replace_notes are mutually exclusive")

    updated: list[UpdatedField] = []
    notes_op: Literal["append", "replace"] | None = None
    with conn:
        row = conn.execute(
            "SELECT name, email, notes FROM customer "
            "WHERE id = ? AND deleted_at IS NULL",
            (customer_id,),
        ).fetchone()
        if row is None:
            return None

        existing_name = row["name"]
        existing_email = row["email"]
        existing_notes = row["notes"] or ""

        # Build the SET clause dynamically so only the touched columns are
        # rewritten. The predicate fragments are literal column-name strings
        # (``"email = ?"`` / ``"notes = ?"`` / ``"name = ?"``) — operator
        # input only lands in the positional ``?`` bindings, never in SQL text.
        # Same-value writes are skipped at field granularity so an operator
        # passing the existing value (e.g. ``--name "X"`` against current "X")
        # does not produce a misleading "changed" audit row.
        set_parts: list[str] = []
        params: list[object] = []
        if new_email is not None and new_email != existing_email:
            set_parts.append("email = ?")
            params.append(new_email)
            updated.append("email")
        if new_name is not None and new_name != existing_name:
            set_parts.append("name = ?")
            params.append(new_name)
            updated.append("name")
        if add_note is not None:
            stamped = f"[{today.isoformat()}] {add_note}"
            new_notes = (
                stamped if not existing_notes else f"{existing_notes}\n{stamped}"
            )
            set_parts.append("notes = ?")
            params.append(new_notes)
            updated.append("notes")
            notes_op = "append"
        elif replace_notes is not None and replace_notes != existing_notes:
            set_parts.append("notes = ?")
            params.append(replace_notes)
            updated.append("notes")
            notes_op = "replace"

        if not set_parts:
            # Either the CLI passed no fields (defense-in-depth: the layer
            # above rejects neither-flag) OR every supplied field already
            # matched the stored value. Either way, no UPDATE + no audit row
            # — the helper's contract is "row exists, nothing to write" so
            # the caller still gets the refreshed detail.
            refreshed_noop = get_customer_detail(conn, customer_id)
            if refreshed_noop is None:
                # The row was visible in the SELECT a few lines up; only a
                # cross-process tombstone race could land here.
                return None
            return UpdateCustomerResult(
                customer=refreshed_noop, updated_fields=(), notes_op=None
            )

        params.append(customer_id)
        # Defense-in-depth: the SELECT above already filtered tombstones,
        # but re-applying ``deleted_at IS NULL`` to the UPDATE prevents a
        # mutation slipping through if a future caller restructures the
        # SELECT-then-UPDATE pair.
        sql = (
            f"UPDATE customer SET {', '.join(set_parts)} "  # nosec B608  # set_parts is a hardcoded column-allowlist ("email = ?", "name = ?", "notes = ?"); only ? placeholders bind values
            "WHERE id = ? AND deleted_at IS NULL"
        )
        cursor = conn.execute(sql, params)
        if cursor.rowcount != 1:
            # Concurrent writer tombstoned the row between SELECT and UPDATE;
            # the ``deleted_at IS NULL`` predicate made our UPDATE a no-op.
            # Match the defense-in-depth pattern in mark_key_paid /
            # correct_customer_tax_id / revoke_api_key — surface the race as
            # exit 3 (not-found) rather than silently succeeding.
            return None

        fields_sorted: tuple[UpdatedField, ...] = tuple(sorted(set(updated)))
        details: dict[str, object] = {"fields": list(fields_sorted)}
        if notes_op is not None:
            # ``notes_op`` distinguishes a destructive ``--notes`` replace
            # from an additive ``--add-note`` append. Forensic readers must
            # be able to detect a notes-column wipe; the discriminator is
            # operator-internal metadata (no PII) so it is safe to record.
            details["notes_op"] = notes_op
        log_event(
            conn,
            actor=actor,
            action="update_customer",
            target_type="customer",
            target_id=customer_id,
            details=details,
        )

    refreshed = get_customer_detail(conn, customer_id)
    if refreshed is None:
        # Shouldn't happen — the row existed inside the transaction and
        # nothing else in this process is racing it. Raise so the CLI
        # surfaces an exit-5 instead of misreading it as "tombstone".
        raise RuntimeError(
            f"update_customer: row vanished after commit for {customer_id!r}"
        )
    return UpdateCustomerResult(
        customer=refreshed, updated_fields=fields_sorted, notes_op=notes_op
    )


@dataclass(frozen=True, slots=True)
class CorrectTaxIdResult:
    """Return payload from ``correct_customer_tax_id``."""

    customer: CustomerDetailRow
    old_tax_id_kind: TaxIdKind
    new_tax_id_kind: TaxIdKind


def correct_customer_tax_id(
    conn: sqlite3.Connection,
    customer_id: str,
    *,
    new_tax_id_encrypted: bytes,
    new_tax_id_kind: TaxIdKind,
    new_tax_id_lookup: bytes,
    reason: str,
    actor: str,
) -> CorrectTaxIdResult | None:
    """Replace ``customer.tax_id_encrypted`` (and ``tax_id_kind``) atomically.

    Story 5.1 Gap 7. Returns ``None`` when the customer is missing or
    tombstoned. The audit row records ``old_kind`` / ``new_kind`` / ``reason``
    — never the tax-ID value (FR37 / NFR15 PII non-leak).

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE and the audit write either both commit or both
    roll back. The encryption is the caller's responsibility (single-Fernet
    invariant: ``crypto.py::encrypt_tax_id``)."""
    from compadre.keys.audit import log_event

    with conn:
        row = conn.execute(
            "SELECT tax_id_kind FROM customer WHERE id = ? AND deleted_at IS NULL",
            (customer_id,),
        ).fetchone()
        if row is None:
            return None
        old_kind = cast(TaxIdKind, row["tax_id_kind"])

        cursor = conn.execute(
            "UPDATE customer "
            "SET tax_id_encrypted = ?, tax_id_kind = ?, tax_id_lookup = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (new_tax_id_encrypted, new_tax_id_kind, new_tax_id_lookup, customer_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "correct_customer_tax_id: update failed to match any row "
                f"after SELECT succeeded for {customer_id!r}"
            )
        log_event(
            conn,
            actor=actor,
            action="correct_tax_id",
            target_type="customer",
            target_id=customer_id,
            details={
                "old_kind": old_kind,
                "new_kind": new_tax_id_kind,
                "reason": reason,
            },
        )

    refreshed = get_customer_detail(conn, customer_id)
    if refreshed is None:
        raise RuntimeError(
            f"correct_customer_tax_id: row vanished after commit for {customer_id!r}"
        )
    return CorrectTaxIdResult(
        customer=refreshed,
        old_tax_id_kind=old_kind,
        new_tax_id_kind=new_tax_id_kind,
    )


def delete_customer_hard(
    conn: sqlite3.Connection,
    customer_id: str,
    *,
    actor: str,
) -> DeleteCustomerResult | None:
    """Hard-delete ``customer_id`` + all associated keys in one transaction.

    Returns ``None`` when the customer is missing or already tombstoned —
    the CLI maps ``None`` to exit 3. Otherwise:

    1. ``DELETE FROM api_key WHERE customer_id = ?`` (rowcount = keys_purged).
    2. ``UPDATE customer SET name = '', email = '', tax_id_encrypted = X'',
       notes = '', deleted_at = <utcnow-iso>`` for the matching id.
    3. ``log_event(..., action='delete_customer', target_type='customer',
       target_id=customer_id, details={'keys_purged': N, 'tax_id_kind': kind})``
       — no personal data in ``details`` (defense-in-depth: audit.py's
       ``_FORBIDDEN_DETAIL_KEYS`` blocks ``name``/``email``/``cpf``/``cnpj``
       / ``tax_id`` already; this helper never attempts to pass them).

    Callers MUST NOT wrap in ``with conn:``. The helper owns the
    transaction so the DELETE, the UPDATE, and the audit write either
    all commit or all roll back.

    ``customer_name`` and ``tax_id_kind`` are captured BEFORE the
    UPDATE so the return payload + audit row both carry the pre-purge
    signal (the name is part of the operator's ConfirmationLine; the
    kind is non-PII metadata FR38 permits).

    Empty-string / zero-length-BLOB writes satisfy the NOT NULL
    constraints on ``name`` / ``email`` / ``tax_id_encrypted``; the
    canonical tombstone signal stays ``deleted_at IS NOT NULL`` (no v002
    migration needed).
    """
    from compadre.keys.audit import log_event

    with conn:
        row = conn.execute(
            "SELECT name, tax_id_kind FROM customer "
            "WHERE id = ? AND deleted_at IS NULL",
            (customer_id,),
        ).fetchone()
        if row is None:
            return None
        customer_name = row["name"]
        tax_id_kind = row["tax_id_kind"]

        # Step 1 — DELETE every associated api_key. ``rowcount`` is the
        # authoritative key-purge count for the ConfirmationLine + audit.
        cursor = conn.execute(
            "DELETE FROM api_key WHERE customer_id = ?",
            (customer_id,),
        )
        keys_purged = cursor.rowcount

        # Step 2 — scrub PII on the customer row + stamp deleted_at.
        # B2: ``tax_id_lookup`` is also nulled so the partial UNIQUE
        # INDEX (``WHERE deleted_at IS NULL AND tax_id_lookup IS NOT
        # NULL``) doesn't pin the digest of a tombstoned tax-ID. This
        # also lets the same CPF be re-registered later — LGPD allows
        # re-onboarding after hard-delete; the tombstone holds no PII.
        now_iso = _utcnow_iso()
        conn.execute(
            "UPDATE customer SET name = '', email = '', "
            "tax_id_encrypted = X'', tax_id_lookup = NULL, "
            "notes = '', deleted_at = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (now_iso, customer_id),
        )

        # Step 3 — one audit row. ``_FORBIDDEN_DETAIL_KEYS`` would
        # reject name/email/tax_id/cpf/cnpj — we pass only structural
        # metadata permitted by FR38.
        log_event(
            conn,
            actor=actor,
            action="delete_customer",
            target_type="customer",
            target_id=customer_id,
            details={"keys_purged": keys_purged, "tax_id_kind": tax_id_kind},
        )

    _touch_auth_cache_sentinel(conn)

    return DeleteCustomerResult(
        customer_id=customer_id,
        customer_name=customer_name,
        keys_purged=keys_purged,
    )


def rotate_encryption_keys(
    conn: sqlite3.Connection,
    *,
    old_key: SecretStr,
    new_key: SecretStr,
    actor: str,
) -> RotateEncryptionKeysResult:
    """Re-encrypt every active customer's tax-ID under ``new_key`` in one transaction.

    1. Begin transaction.
    2. ``SELECT id, tax_id_encrypted FROM customer WHERE deleted_at IS NULL``
       — tombstoned rows (zero-length BLOB ciphertext) are skipped because
       ``Fernet.decrypt(b"")`` raises ``InvalidToken`` and the row holds no
       PII anyway.
    3. For each row: ``decrypt_tax_id(old)`` → ``encrypt_tax_id(canonical,
       new)`` → ``UPDATE customer SET tax_id_encrypted = ? WHERE id = ?``.
    4. ``log_event(action='rotate_encryption_key',
       target_type='registry', target_id=None,
       details={'rows_re_encrypted': N})``.
    5. Commit.

    Callers MUST NOT wrap in ``with conn:``. The helper owns the
    transaction so all UPDATEs and the audit write either all commit
    or all roll back. Mid-iteration failures (decrypt error from a
    corrupted row, disk full, ``KeyboardInterrupt``) propagate as
    exceptions; the ``with conn:`` rolls back and every row retains
    its **original** ciphertext — the FR36 "no window of unreadable
    data" guarantee.

    The decrypted canonical plaintext exists only inside the loop's
    local frame and is overwritten on each iteration — no cache, no
    return-channel exposure. The audit ``details`` carry only the
    structural count; ``_FORBIDDEN_DETAIL_KEYS`` rejects any attempt
    to pass tax-ID material.
    """
    from compadre.keys.audit import log_event
    from compadre.keys.crypto import (
        compute_tax_id_lookup,
        decrypt_tax_id,
        encrypt_tax_id,
    )

    occurred_at = _utcnow_iso()
    rows_re_encrypted = 0

    with conn:
        # Materialise the row set before issuing any UPDATE. SQLite on
        # some platforms/versions raises ``OperationalError("database
        # table is locked")`` when a write is issued on the same
        # connection that still holds an open SELECT cursor. At the
        # NFR35 reference scale (1000 customers, ~150 B per ciphertext)
        # this is ~150 KB — trivially safe in memory.
        rows = conn.execute(
            "SELECT id, tax_id_encrypted FROM customer WHERE deleted_at IS NULL"
        ).fetchall()

        for row in rows:
            canonical = decrypt_tax_id(bytes(row["tax_id_encrypted"]), old_key)
            new_ciphertext = encrypt_tax_id(canonical, new_key)
            # B2: the tax_id_lookup column is HMAC keyed by the encryption
            # key, so a key rotation MUST recompute it in the same
            # transaction or the partial UNIQUE INDEX would silently
            # accept duplicates (old-key HMAC collides with new-key HMAC
            # only by chance). The recompute is free in I/O terms — we
            # already have the canonical plaintext in scope from decrypt.
            new_lookup = compute_tax_id_lookup(canonical, new_key)
            # ``rowcount`` instead of ``+= 1`` so the audit total stays
            # honest if a concurrent writer tombstones a row between
            # the SELECT and this UPDATE — the ``deleted_at IS NULL``
            # predicate would then match zero rows and the counter
            # must reflect that.
            cursor = conn.execute(
                "UPDATE customer "
                "SET tax_id_encrypted = ?, tax_id_lookup = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (new_ciphertext, new_lookup, row["id"]),
            )
            rows_re_encrypted += cursor.rowcount

        log_event(
            conn,
            actor=actor,
            action="rotate_encryption_key",
            target_type="registry",
            target_id=None,
            details={"rows_re_encrypted": rows_re_encrypted},
            occurred_at=occurred_at,
        )

    return RotateEncryptionKeysResult(
        rows_re_encrypted=rows_re_encrypted,
        occurred_at=occurred_at,
    )


def list_customers(
    conn: sqlite3.Connection,
    *,
    include_deleted: bool,
) -> list[CustomerListRow]:
    """Return customer rows sorted by ``registered_at ASC``.

    Tombstoned customers (``deleted_at IS NOT NULL``) are filtered out
    unless ``include_deleted`` is True. The ``key_count`` column is
    computed via a single ``LEFT JOIN api_key`` — no N+1 per-row counts.
    """
    cursor = conn.execute(
        "SELECT "
        "c.id, c.name, c.email, c.tax_id_kind, c.notes, "
        "c.registered_at, c.deleted_at, "
        "COUNT(k.id) AS key_count "
        "FROM customer AS c "
        "LEFT JOIN api_key AS k ON k.customer_id = c.id "
        "WHERE (? OR c.deleted_at IS NULL) "
        "GROUP BY c.id "
        "ORDER BY c.registered_at ASC",
        (1 if include_deleted else 0,),
    )
    rows: list[CustomerListRow] = []
    for row in cursor:
        rows.append(
            CustomerListRow(
                id=row["id"],
                name=row["name"],
                email=row["email"],
                tax_id_kind=cast(TaxIdKind, row["tax_id_kind"]),
                key_count=int(row["key_count"]),
                registered_at=row["registered_at"],
                deleted_at=row["deleted_at"],
                notes=row["notes"] or "",
            )
        )
    return rows


def list_api_keys(
    conn: sqlite3.Connection,
    *,
    status: CustomerStatus | None = None,
    billing_mode: BillingMode | None = None,
    customer_id: str | None = None,
    paid_until_lte: str | None = None,
) -> list[ApiKeyListRow]:
    """Return api_key rows with optional filters, sorted by ``created_at ASC``.

    Each non-None filter contributes one ``?`` placeholder to the WHERE
    clause; the predicate literals themselves are hard-coded so operator
    input never lands in SQL text. The ``paid_until`` filter matches both
    NULL and ``paid_until <= ?`` rows (NULL treated as "not yet billed"
    so the operator's "who owes me?" query catches trial-window keys).
    """
    # WHERE 1=1 + AND-appended predicates removes the f-string SQL assembly
    # entirely (defensive: a future contributor cannot accidentally splice
    # operator input via the where_clause concatenation pattern).
    sql_parts: list[str] = [
        "SELECT k.id, k.customer_id, c.name AS customer_name, "
        "k.key_prefix, k.status, k.billing_mode, k.rate_limit_rpm, "
        "k.price_monthly, k.paid_until, k.created_at, k.last_used_at, k.notes "
        "FROM api_key AS k JOIN customer AS c ON c.id = k.customer_id "
        "WHERE 1=1"
    ]
    params: list[object] = []
    if status is not None:
        sql_parts.append(" AND k.status = ?")
        params.append(status)
    if billing_mode is not None:
        sql_parts.append(" AND k.billing_mode = ?")
        params.append(billing_mode)
    if customer_id is not None:
        sql_parts.append(" AND k.customer_id = ?")
        params.append(customer_id)
    if paid_until_lte is not None:
        sql_parts.append(" AND (k.paid_until IS NULL OR k.paid_until <= ?)")
        params.append(paid_until_lte)
    sql_parts.append(" ORDER BY k.created_at ASC")
    sql = "".join(sql_parts)
    cursor = conn.execute(sql, params)
    rows: list[ApiKeyListRow] = []
    for row in cursor:
        rows.append(
            ApiKeyListRow(
                id=row["id"],
                customer_id=row["customer_id"],
                customer_name=row["customer_name"],
                key_prefix=row["key_prefix"],
                status=cast(CustomerStatus, row["status"]),
                billing_mode=cast(BillingMode, row["billing_mode"]),
                rate_limit_rpm=int(row["rate_limit_rpm"]),
                price_monthly=float(row["price_monthly"]),
                paid_until=row["paid_until"],
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
                notes=row["notes"] or "",
            )
        )
    return rows


def is_first_run(conn: sqlite3.Connection) -> bool:
    """Return True when both ``customer`` and ``api_key`` are empty.

    Drives the UX-DR13 first-run-hint policy for ``list-customers``: the
    create-customer bootstrap hint only shows when the operator has
    never used the system at all. A tombstoned customer row counts as
    prior use, so purged-only DBs return False.
    """
    customers = conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0]
    if customers:
        return False
    keys = conn.execute("SELECT COUNT(*) FROM api_key").fetchone()[0]
    return not keys


def is_first_run_for_keys(conn: sqlite3.Connection) -> bool:
    """Return True when the ``api_key`` table is empty.

    Drives the ``list-keys`` first-run-hint policy: the issue-key
    bootstrap hint fires whenever no keys have been issued yet,
    regardless of whether customers exist — AC7 treats both the
    "customers exist, zero keys" and "completely empty DB" cases
    as first-run for the keys workflow.
    """
    keys = conn.execute("SELECT COUNT(*) FROM api_key").fetchone()[0]
    return not keys


def count_tombstones(conn: sqlite3.Connection) -> int:
    """Return the count of tombstoned customers (``deleted_at IS NOT NULL``)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM customer WHERE deleted_at IS NOT NULL"
    ).fetchone()
    return int(row[0])


def count_active_customers(conn: sqlite3.Connection) -> int:
    """Return the count of non-tombstoned customers (``deleted_at IS NULL``).

    Companion to ``count_tombstones`` — together they partition the
    ``customer`` table for ``health`` summary rendering (Story 3.4).
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM customer WHERE deleted_at IS NULL"
    ).fetchone()
    return int(row[0])


def count_keys_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Return ``{status: count}`` across the ``api_key.status`` enum.

    Always returns the full fixed-shape set
    ``{"active": N, "suspended": N, "revoked": N}`` — statuses absent
    from the GROUP BY result render as ``0``. The fixed-shape contract
    lets the renderer iterate without branching on missing keys.
    """
    base: dict[str, int] = {"active": 0, "suspended": 0, "revoked": 0}
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM api_key GROUP BY status"
    ).fetchall()
    for row in rows:
        status = str(row[0])
        if status in base:
            base[status] = int(row[1])
    return base


def get_customer_detail(
    conn: sqlite3.Connection,
    customer_id: str,
) -> CustomerDetailRow | None:
    """Return the active customer detail row (with ``key_count``) or None.

    Tombstoned customers (``deleted_at IS NOT NULL``) are treated as missing,
    matching ``get_customer``. The ``key_count`` column is produced by a
    single ``LEFT JOIN api_key`` — no N+1 follow-up count.
    """
    row = conn.execute(
        "SELECT "
        "c.id, c.name, c.email, c.tax_id_kind, c.notes, "
        "c.registered_at, "
        "COUNT(k.id) AS key_count "
        "FROM customer AS c "
        "LEFT JOIN api_key AS k ON k.customer_id = c.id "
        "WHERE c.id = ? AND c.deleted_at IS NULL "
        "GROUP BY c.id",
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    return CustomerDetailRow(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        tax_id_kind=cast(TaxIdKind, row["tax_id_kind"]),
        key_count=int(row["key_count"]),
        registered_at=row["registered_at"],
        notes=row["notes"] or "",
    )


def _row_to_key_detail(row: sqlite3.Row, *, today: date) -> KeyDetailRow:
    """Project a joined ``api_key + customer`` row to a ``KeyDetailRow``."""
    created_head = row["created_at"][:10]
    age_days = (today - date.fromisoformat(created_head)).days
    return KeyDetailRow(
        id=row["id"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        key_prefix=row["key_prefix"],
        status=cast(CustomerStatus, row["status"]),
        billing_mode=cast(BillingMode, row["billing_mode"]),
        rate_limit_rpm=int(row["rate_limit_rpm"]),
        price_monthly=float(row["price_monthly"]),
        paid_until=row["paid_until"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        age_days=age_days,
        notes=row["notes"] or "",
    )


_KEY_DETAIL_SELECT = (
    "SELECT k.id, k.customer_id, c.name AS customer_name, "
    "k.key_prefix, k.status, k.billing_mode, k.rate_limit_rpm, "
    "k.price_monthly, k.paid_until, k.created_at, k.last_used_at, k.notes "
    "FROM api_key AS k JOIN customer AS c ON c.id = k.customer_id "
)


def get_key_detail(
    conn: sqlite3.Connection,
    key_prefix: str,
    *,
    today: date,
) -> KeyDetailRow | list[KeyDetailRow] | None:
    """Return a key's detail row by exact ``key_prefix`` match.

    Returns ``None`` when no rows match, a single ``KeyDetailRow`` when
    exactly one matches, and a ``list[KeyDetailRow]`` (length ≥ 2) on the
    vanishingly-rare collision. The distinction is a domain signal the
    caller branches on — not an internal detail to flatten away.
    """
    rows = conn.execute(
        _KEY_DETAIL_SELECT + "WHERE k.key_prefix = ?",
        (key_prefix,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return _row_to_key_detail(rows[0], today=today)
    return [_row_to_key_detail(r, today=today) for r in rows]


def get_key_detail_by_id(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    today: date,
) -> KeyDetailRow | None:
    """Return a key's detail row by exact ``api_key.id`` match (or None)."""
    row = conn.execute(
        _KEY_DETAIL_SELECT + "WHERE k.id = ?",
        (key_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_key_detail(row, today=today)


def lookup_key_auth(
    conn: sqlite3.Connection,
    key_hash: str,
) -> AuthPayload | None:
    """Return the minimal auth payload for ``key_hash``, or None on miss.

    Used by ``verify_api_key`` (in ``compadre.keys.auth``). Never writes
    (auth path is read-only). Uses the existing ``idx_api_key_hash``
    UNIQUE index — the SELECT is O(1).
    """
    row = conn.execute(
        "SELECT id, customer_id, status, billing_mode, rate_limit_rpm, "
        "paid_until FROM api_key WHERE key_hash = ? LIMIT 1",
        (key_hash,),
    ).fetchone()
    if row is None:
        return None
    return AuthPayload(
        customer_id=row["customer_id"],
        key_id=row["id"],
        status=cast(CustomerStatus, row["status"]),
        billing_mode=cast(BillingMode, row["billing_mode"]),
        rate_limit_rpm=int(row["rate_limit_rpm"]),
        paid_until=row["paid_until"],
    )


def list_audit_events(
    conn: sqlite3.Connection,
    target_ids: Sequence[str],
    limit: int,
) -> list[AuditEventRow]:
    """Return the newest-first audit slice for ``target_ids`` capped at ``limit``.

    ``target_ids`` contains the customer id alone (key path) or the customer
    id plus its key ids (customer path) — see ``resolve_show_target`` and
    ``show_cmd`` for the scoping policy. Placeholders for the ``IN`` clause
    are produced programmatically from the sequence length; operator input
    only lands in positional ``?`` bindings, never in the SQL text.

    Raises ``ValueError`` when ``limit`` is outside ``[1, 1000]`` (defensive
    — the Typer layer validates too, but a bare helper should not trust its
    caller). Using ``raise`` rather than ``assert`` so ``-O`` mode keeps the
    check.
    """
    if not 1 <= limit <= 1000:
        raise ValueError(f"limit must be between 1 and 1000, got {limit}")
    if not target_ids:
        return []
    placeholders = ",".join("?" * len(target_ids))
    sql = (
        "SELECT id, occurred_at, actor, action, target_type, target_id, "
        "details_json FROM audit_log "
        f"WHERE target_id IN ({placeholders}) "  # nosec B608  # placeholders is ",".join("?" * n) — only ? tokens; operator input binds via positional ? params
        "ORDER BY occurred_at DESC, id DESC LIMIT ?"
    )
    cursor = conn.execute(sql, (*target_ids, limit))
    return [
        AuditEventRow(
            id=int(r["id"]),
            occurred_at=r["occurred_at"],
            actor=r["actor"],
            action=r["action"],
            target_type=r["target_type"],
            target_id=r["target_id"],
            details_json=r["details_json"],
        )
        for r in cursor
    ]


def _utc_iso_to_billing_date(iso: str, tz: ZoneInfo) -> date | None:
    """Convert an ISO-8601 UTC timestamp to a date in ``tz``.

    ``audit_log.occurred_at`` and ``api_key.created_at`` are stored as UTC
    ISO strings (``_utcnow_iso`` / ``insert_api_key``). ``revenue --as-of``
    operates in ``BILLING_TIMEZONE`` semantics — the operator's "today" is
    the billing-TZ midnight boundary, not UTC's. Truncating the UTC string
    to ``[:10]`` would drift up to ±1 day near the midnight boundary in
    timezones west of UTC. This helper does the proper conversion.

    Returns ``None`` when ``iso`` cannot be parsed; the caller logs a
    warning and skips the row rather than aborting the replay.
    """
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        # Stored values are written via ``_utcnow_iso`` ("...Z" suffix), so
        # naive timestamps are an out-of-band insertion path. Treat them as
        # UTC (the historical contract) rather than rejecting outright.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).date()


def earliest_audit_date(conn: sqlite3.Connection, tz: ZoneInfo) -> date | None:
    """Return the date of the oldest ``audit_log`` row in ``tz``, or ``None``.

    Used by ``revenue --as-of`` (Story 5.1 Gap 8) to validate that the
    operator-supplied replay date isn't earlier than any recorded history.
    Pulls only the ``MIN(occurred_at)`` aggregate so the query is O(index)
    on ``idx_audit_occurred``. The aggregate value is converted from UTC
    to ``tz`` before truncating to date so the lower-bound check honours
    operator-local midnight boundaries (review-pass fix for the UTC↔
    billing-TZ drift)."""
    row = conn.execute("SELECT MIN(occurred_at) FROM audit_log").fetchone()
    if row is None or row[0] is None:
        return None
    return _utc_iso_to_billing_date(str(row[0]), tz)


def reconstruct_key_state_at(
    conn: sqlite3.Connection,
    as_of: date,
    *,
    tz: ZoneInfo,
) -> list[ApiKeyListRow]:
    """Return the ``api_key`` row set as it would have looked on ``as_of``.

    Story 5.1 Gap 8 — ``revenue --as-of`` audit replay.

    Forward-replay over ``audit_log`` rows up to ``as_of``, applied to the
    keys that already existed on that date. Returns ``ApiKeyListRow`` rows
    with reconstructed ``status`` / ``paid_until`` / ``price_monthly`` so
    the caller can feed them straight into ``_bucketize_for_revenue``.

    Audit timestamps (``occurred_at`` / ``created_at``) are converted from
    UTC to ``tz`` (operator billing timezone) before truncating to date,
    so the comparison against ``as_of`` honours the operator's midnight
    boundary rather than UTC's.

    **Audit coverage:** ``status`` and ``paid_until`` are fully tracked
    (``mark_paid`` / ``suspend_key`` / ``reactivate_key`` / ``revoke_key``).
    ``price_monthly`` is fully replayable from Story 5.1 review-pass forward:
    both ``mark_paid`` (Gap 4) AND ``update_key`` (review-pass) audit rows
    carry ``{price_monthly_changed, old, new}`` when the price moves. The
    replay snapshots each key's ``price_monthly`` to its current value, then
    walks every post-as_of price-change event (oldest first) and rewinds
    using the recorded ``old`` value. Keys whose price moved only via
    pre-Story-5.1-review audit rows that lack ``old/new`` will show the
    current price (the legacy fallback); flag in CLI output if needed.

    ``billing_mode`` is recorded on ``issue_key`` and is treated as
    immutable (no CLI verb mutates it post-issue).

    Keys created AFTER ``as_of`` (in ``tz``) are excluded from the result
    entirely (they didn't exist yet)."""
    all_rows = list_api_keys(conn)
    if not all_rows:
        return []
    rows_by_id = {row.id: row for row in all_rows}

    valid_ids: list[str] = []
    for row in all_rows:
        created_date = _utc_iso_to_billing_date(row.created_at, tz)
        if created_date is None:
            # An unparseable ``created_at`` would silently drop the key
            # from MRR replay. Log a breadcrumb so operators can detect
            # corrupt data rather than wonder why a known key is missing.
            _logger.warning(
                "reconstruct_key_state_at: key %s has unparseable "
                "created_at=%r; excluded from replay",
                row.id,
                row.created_at,
            )
            continue
        if created_date <= as_of:
            valid_ids.append(row.id)

    if not valid_ids:
        return []

    placeholders = ",".join("?" * len(valid_ids))
    sql = (
        "SELECT id, occurred_at, action, target_id, details_json "
        "FROM audit_log "
        f"WHERE target_id IN ({placeholders}) "  # nosec B608  # placeholders is ",".join("?" * n) — only ? tokens; key ids bind via positional params
        "ORDER BY occurred_at ASC, id ASC"
    )
    cursor = conn.execute(sql, tuple(valid_ids))
    events = list(cursor)

    snapshots: dict[str, dict[str, object]] = {}
    for kid in valid_ids:
        row = rows_by_id[kid]
        snapshots[kid] = {
            "status": "active",
            "billing_mode": row.billing_mode,
            "paid_until": None,
            "price_monthly": row.price_monthly,
        }

    # First pass — for every key, find the OLDEST post-as_of price-change
    # event (``mark_paid`` OR ``update_key`` carrying ``price_monthly_changed``)
    # and rewind the snapshot's price to its ``old`` value. The snapshot
    # starts from the live row's price (today's value), so undoing post-
    # as_of changes lands on the price at as_of.
    bootstrap_price: dict[str, float] = {}
    for ev in events:
        kid = ev["target_id"]
        if kid not in snapshots or kid in bootstrap_price:
            continue
        if ev["action"] not in ("mark_paid", "update_key"):
            continue
        ev_date = _utc_iso_to_billing_date(str(ev["occurred_at"]), tz)
        if ev_date is None or ev_date <= as_of:
            continue
        if not ev["details_json"]:
            continue
        try:
            bootstrap_details = json.loads(ev["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(bootstrap_details, dict)
            and bootstrap_details.get("price_monthly_changed")
            and "old" in bootstrap_details
        ):
            old = bootstrap_details["old"]
            if isinstance(old, (int, float)):
                bootstrap_price[kid] = float(old)
    for kid, price in bootstrap_price.items():
        snapshots[kid]["price_monthly"] = price

    # Second pass — forward-replay events at or before as_of for status,
    # paid_until, and the within-window price moves. Price moves before
    # as_of are already reflected in the live row's price (we initialized
    # from current); the bootstrap pass above only undoes POST-as_of moves,
    # so we leave snap.price_monthly unchanged here for events at-or-before
    # as_of unless a ``new`` value is recorded — which we use to verify the
    # snapshot lands at the right intermediate state for tests / forensics.
    for ev in events:
        kid = ev["target_id"]
        if kid not in snapshots:
            continue
        ev_date = _utc_iso_to_billing_date(str(ev["occurred_at"]), tz)
        if ev_date is None:
            continue
        if ev_date > as_of:
            continue
        snap = snapshots[kid]
        details: dict[str, object] = {}
        if ev["details_json"]:
            try:
                parsed = json.loads(ev["details_json"])
                if isinstance(parsed, dict):
                    details = parsed
            except json.JSONDecodeError:
                pass
        action = ev["action"]
        if action == "mark_paid":
            if "paid_until" in details:
                # Schema NOT NULL/TEXT shields the live row, but a corrupt
                # or out-of-band audit row could carry an int / null here;
                # _bucketize_for_revenue calls date.fromisoformat on this
                # field downstream and would TypeError on non-string input.
                paid_until_value = details["paid_until"]
                if paid_until_value is None or isinstance(paid_until_value, str):
                    snap["paid_until"] = paid_until_value
            if details.get("reactivated"):
                snap["status"] = "active"
        elif action == "suspend_key":
            snap["status"] = "suspended"
        elif action == "reactivate_key":
            snap["status"] = "active"
        elif action == "revoke_key":
            snap["status"] = "revoked"
        # ``update_key`` mutates only price/rpm/notes — none of those affect
        # the bucket logic except price, which is handled by the bootstrap
        # pass above. No second-pass arm needed.

    result: list[ApiKeyListRow] = []
    for kid, snap in snapshots.items():
        row = rows_by_id[kid]
        result.append(
            ApiKeyListRow(
                id=row.id,
                customer_id=row.customer_id,
                customer_name=row.customer_name,
                key_prefix=row.key_prefix,
                status=cast(CustomerStatus, snap["status"]),
                billing_mode=cast(BillingMode, snap["billing_mode"]),
                rate_limit_rpm=row.rate_limit_rpm,
                price_monthly=cast(float, snap["price_monthly"]),
                paid_until=cast("str | None", snap["paid_until"]),
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                notes=row.notes,
            )
        )
    return result


def resolve_show_target(
    conn: sqlite3.Connection,
    raw: str,
    *,
    today: date,
) -> ShowTarget:
    """Discriminate operator input into a ``ShowTarget``.

    Branching is shape-first: the ``cust_`` / ``key_`` / ``cmpd_live_``
    prefixes unambiguously tag the intent. The bare-prefix fallback accepts
    any 1..8 character input; shorter inputs simply miss the exact-match
    query (we do not ``LIKE`` — partial matches open an enumeration side
    channel and the operator has ``list-keys`` for discovery).
    """
    if raw.startswith("cust_"):
        detail = get_customer_detail(conn, raw)
        if detail is None:
            return ShowTarget(kind="not_found", value=None)
        return ShowTarget(kind="customer", value=detail)

    if raw.startswith("cmpd_live_"):
        # Convenience for an operator who pasted the full plaintext: strip
        # the advertised prefix and use the first 8 chars of the remainder
        # as the key_prefix candidate, then resolve through the same
        # bare-prefix lookup as the fallback branch below.
        candidate = raw.removeprefix("cmpd_live_")[:8]
        result = get_key_detail(conn, candidate, today=today)
        if result is None:
            return ShowTarget(kind="not_found", value=None)
        if isinstance(result, list):
            return ShowTarget(kind="key_ambiguous", value=result)
        return ShowTarget(kind="key_single", value=result)

    if raw.startswith("key_"):
        by_id = get_key_detail_by_id(conn, raw, today=today)
        if by_id is None:
            return ShowTarget(kind="not_found", value=None)
        return ShowTarget(kind="key_single", value=by_id)

    result = get_key_detail(conn, raw, today=today)
    if result is None:
        return ShowTarget(kind="not_found", value=None)
    if isinstance(result, list):
        return ShowTarget(kind="key_ambiguous", value=result)
    return ShowTarget(kind="key_single", value=result)


def insert_api_key(
    conn: sqlite3.Connection,
    record: ApiKeyRecord,
    *,
    actor: str,
) -> None:
    """Insert an api_key row and its audit row in a single transaction.

    Callers must NOT open a surrounding `with conn:` — this function opens
    the transaction itself so the key insert and the audit row either both
    commit or both roll back (Story 1.2 AC8 invariant).

    `sqlite3.IntegrityError` from the `key_hash UNIQUE` constraint (AC6)
    propagates to the caller, which maps it to the hash-collision error.
    """
    from compadre.keys.audit import log_event

    created_at_iso = record.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_used_at_iso = (
        record.last_used_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if record.last_used_at is not None
        else None
    )
    with conn:
        conn.execute(
            "INSERT INTO api_key ("
            "id, customer_id, key_hash, key_prefix, rate_limit_rpm, status, "
            "billing_mode, price_monthly, paid_until, created_at, last_used_at, "
            "notes"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.customer_id,
                record.key_hash,
                record.key_prefix,
                record.rate_limit_rpm,
                record.status,
                record.billing_mode,
                record.price_monthly,
                record.paid_until,
                created_at_iso,
                last_used_at_iso,
                record.notes,
            ),
        )
        log_event(
            conn,
            actor=actor,
            action="issue_key",
            target_type="api_key",
            target_id=record.id,
            details={
                "billing_mode": record.billing_mode,
                "rate_limit_rpm": record.rate_limit_rpm,
            },
        )


def update_api_key(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    new_rpm: int | None,
    new_price: float | None,
    new_notes: str | None,
    actor: str,
    today: date,
) -> UpdateKeyResult | None:
    """Apply the rpm/price/notes mutation inside a single transaction.

    Returns ``None`` when the row is missing. Raises ``ValueError`` when
    the row is revoked or when ``new_price`` is set on a complimentary
    key (the CLI maps the second case to an exit-2 usage error — the
    helper's ``ValueError`` is a defensive safety net behind the CLI's
    upstream mutex check).

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE and audit write either both commit or both
    roll back. Same contract as ``insert_customer`` / ``insert_api_key`` /
    ``update_customer``.

    ``today`` is the operator-TZ date forwarded to ``get_key_detail_by_id``
    so the post-commit re-read is clock-seamed (tests freeze today via
    monkeypatch instead of the helper touching ``date.today()``).
    """
    from compadre.keys.audit import log_event

    updated: list[UpdateKeyField] = []
    old_rpm: int | None = None
    old_price: float | None = None
    with conn:
        row = conn.execute(
            "SELECT status, billing_mode, rate_limit_rpm, price_monthly, notes "
            "FROM api_key WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "revoked":
            raise ValueError("cannot update a revoked key")
        if row["billing_mode"] == "complimentary" and new_price is not None:
            raise ValueError("cannot set --price on a complimentary key")

        old_rpm = int(row["rate_limit_rpm"])
        old_price = float(row["price_monthly"])
        old_notes = row["notes"] or ""

        # Build the SET clause dynamically so only the touched columns are
        # rewritten. The predicate fragments are literal column-name strings
        # (``"rate_limit_rpm = ?"``) — operator input only lands in the
        # positional ``?`` bindings, never in SQL text.
        # B25 — same-value writes are skipped at field granularity (mirrors
        # ``update_customer``). An operator passing the existing value
        # (e.g. ``--rpm 120`` against current 120, or ``--notes "x"`` against
        # current "x") MUST NOT produce a misleading audit row.
        set_parts: list[str] = []
        params: list[object] = []
        if new_rpm is not None and int(new_rpm) != old_rpm:
            set_parts.append("rate_limit_rpm = ?")
            params.append(new_rpm)
            updated.append("rpm")
        if new_price is not None and float(new_price) != old_price:
            set_parts.append("price_monthly = ?")
            params.append(float(new_price))
            updated.append("price")
        if new_notes is not None and new_notes != old_notes:
            set_parts.append("notes = ?")
            params.append(new_notes)
            updated.append("notes")

        if not set_parts:
            # Either the CLI passed no fields (defense-in-depth: the layer
            # above rejects neither-flag) OR every supplied field already
            # matched the stored value. Either way, no UPDATE + no audit row
            # — the helper's contract is "row exists, nothing to write" so
            # the caller still gets the refreshed detail.
            refreshed_noop = get_key_detail_by_id(conn, key_id, today=today)
            if refreshed_noop is None:
                # The row was visible in the SELECT a few lines up; only a
                # cross-process race could land here.
                return None
            return UpdateKeyResult(
                key=refreshed_noop,
                updated_fields=(),
                changes=KeyFieldDeltas(
                    rpm_old=None, rpm_new=None, price_old=None, price_new=None
                ),
            )

        params.append(key_id)
        # Defense-in-depth: the SELECT filtered revoked rows, but re-applying
        # the predicate to the UPDATE prevents a mutation slipping through
        # if a future caller restructures the SELECT-then-UPDATE pair.
        sql = (
            f"UPDATE api_key SET {', '.join(set_parts)} "  # nosec B608  # set_parts is a hardcoded column-allowlist ("rate_limit_rpm = ?", "notes = ?", ...); only ? placeholders bind values
            "WHERE id = ? AND status != 'revoked'"
        )
        conn.execute(sql, params)

        fields_sorted: tuple[UpdateKeyField, ...] = tuple(sorted(set(updated)))
        update_details: dict[str, object] = {"fields": list(fields_sorted)}
        # Carry old/new for ``price_monthly`` so the audit log is replayable
        # by ``reconstruct_key_state_at`` (revenue --as-of). Forward-only:
        # the field is additive, pre-5.1 audit rows simply omit it. The
        # ``price_monthly_changed`` discriminator mirrors the ``mark_paid``
        # convention so the replayer treats both call sites identically.
        if (
            new_price is not None
            and old_price is not None
            and float(old_price) != float(new_price)
        ):
            update_details["price_monthly_changed"] = True
            update_details["old"] = float(old_price)
            update_details["new"] = float(new_price)
        # B8 — carry rpm old/new symmetrically with price. Useful for
        # forensic replay of throughput contract changes (relevant to
        # at-risk and capacity-planning queries). ``rpm_old/rpm_new``
        # avoids colliding with the ``old/new`` keys reserved for the
        # price-replay path.
        if new_rpm is not None and old_rpm is not None and int(old_rpm) != int(new_rpm):
            update_details["rpm_changed"] = True
            update_details["rpm_old"] = int(old_rpm)
            update_details["rpm_new"] = int(new_rpm)
        # B8 — flag (not full content) for notes. Audit reader can see
        # the row mutation without us writing operator-internal note
        # text into the audit_log column (which is also how
        # ``update_customer notes_op`` keeps the discipline).
        if new_notes is not None:
            update_details["notes_changed"] = True
        log_event(
            conn,
            actor=actor,
            action="update_key",
            target_type="api_key",
            target_id=key_id,
            details=update_details,
        )

    _touch_auth_cache_sentinel(conn)

    refreshed = get_key_detail_by_id(conn, key_id, today=today)
    if refreshed is None:
        raise RuntimeError(f"update_api_key: row vanished after commit for {key_id!r}")

    deltas = KeyFieldDeltas(
        rpm_old=old_rpm if new_rpm is not None else None,
        rpm_new=new_rpm if new_rpm is not None else None,
        price_old=old_price if new_price is not None else None,
        price_new=float(new_price) if new_price is not None else None,
    )
    return UpdateKeyResult(key=refreshed, updated_fields=fields_sorted, changes=deltas)


def rotate_api_key(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    new_plaintext: str,
    actor: str,
) -> RotateKeyResult | None:
    """Rotate an API key's plaintext — new hash, new prefix, same ``id``.

    Returns ``None`` when the row is missing. Raises ``ValueError`` when
    the row is revoked (rotation is rejected; the CLI maps to exit 3).
    Propagates ``sqlite3.IntegrityError`` when the freshly-computed hash
    collides with an existing row's ``key_hash`` (AC12 — exit 5 with a
    retry hint at the CLI layer).

    Callers MUST NOT wrap this in ``with conn:``. The UPDATE and audit
    write either both commit or both roll back — if the audit write
    fails the old ``key_hash`` stays authoritative and no plaintext has
    been disclosed yet.

    No ``today`` parameter: the helper assembles the refreshed
    ``ApiKeyRecord`` from the pre-UPDATE SELECT row plus the new hash /
    prefix in-memory, skipping a second DB round-trip. The ``ApiKeyRecord``
    type has no date-derived fields (``age_days`` lives on ``KeyDetailRow``,
    which the rotate flow does not need — it renders a ``KeyPanel``).
    """
    from compadre.keys.audit import log_event
    from compadre.keys.tokens import hash_api_key, key_prefix_from_plaintext

    new_hash = hash_api_key(new_plaintext)
    new_prefix = key_prefix_from_plaintext(new_plaintext)

    with conn:
        row = conn.execute(
            "SELECT k.id, k.customer_id, k.key_prefix, k.rate_limit_rpm, "
            "k.status, k.billing_mode, k.price_monthly, k.paid_until, "
            "k.created_at, k.notes, c.name AS customer_name "
            "FROM api_key AS k JOIN customer AS c ON c.id = k.customer_id "
            "WHERE k.id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "revoked":
            raise ValueError("cannot rotate a revoked key")

        old_prefix = row["key_prefix"]

        conn.execute(
            "UPDATE api_key SET key_hash = ?, key_prefix = ?, "
            "last_used_at = NULL WHERE id = ?",
            (new_hash, new_prefix, key_id),
        )
        log_event(
            conn,
            actor=actor,
            action="rotate_key",
            target_type="api_key",
            target_id=key_id,
            details={"key_prefix_old": old_prefix, "key_prefix_new": new_prefix},
        )

    _touch_auth_cache_sentinel(conn)

    created_at_dt = datetime.fromisoformat(row["created_at"])
    refreshed = ApiKeyRecord(
        id=row["id"],
        customer_id=row["customer_id"],
        key_hash=new_hash,
        key_prefix=new_prefix,
        rate_limit_rpm=int(row["rate_limit_rpm"]),
        status=cast("CustomerStatus", row["status"]),
        billing_mode=cast("BillingMode", row["billing_mode"]),
        price_monthly=float(row["price_monthly"]),
        paid_until=row["paid_until"],
        created_at=created_at_dt,
        last_used_at=None,
        notes=row["notes"] or "",
    )
    return RotateKeyResult(
        record=refreshed, customer_name=row["customer_name"], old_prefix=old_prefix
    )


def revoke_api_key(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    reason: str,
    actor: str,
    today: date,
) -> RevokeKeyResult | None:
    """Mark the ``api_key`` row revoked inside a single transaction.

    Returns ``None`` when the row is missing. Raises ``ValueError`` when
    the row is already revoked (revocation is terminal; the CLI maps to
    exit 3).

    Callers MUST NOT wrap this in ``with conn:``. The UPDATE and audit
    write either both commit or both roll back.

    ``today`` is forwarded to ``get_key_detail_by_id`` so the post-commit
    re-read that populates the returned ``KeyDetailRow`` is clock-seamed.
    The signature deviates from the Story 1.6 code sketch (which omitted
    ``today``) because the re-read needs an operator-TZ date to compute
    ``age_days`` without touching ``date.today()`` — keeping tests
    clock-free matches the ``update_api_key`` and ``update_customer``
    precedent.

    ``reason`` is shape-validated here (NUL / LF / CR / strip-empty /
    1024-char cap) so the audit-row contract is enforced at every call-
    site — not only from the CLI. Raises ``ValueError`` for any violation.
    The CLI performs the same checks upfront (exit 2) so these are
    defense-in-depth for non-CLI callers.
    """
    from compadre.keys.audit import log_event

    if "\x00" in reason:
        raise ValueError("reason contains NUL byte")
    if "\n" in reason or "\r" in reason:
        raise ValueError("reason must be a single line")
    reason = reason.strip()
    if not reason:
        raise ValueError("reason must be non-empty")
    if len(reason) > 1024:
        raise ValueError("reason exceeds 1024 characters")

    with conn:
        row = conn.execute(
            "SELECT status FROM api_key WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "revoked":
            raise ValueError("already revoked")

        cursor = conn.execute(
            "UPDATE api_key SET status = 'revoked' "
            "WHERE id = ? AND status != 'revoked'",
            (key_id,),
        )
        # Guard against a concurrent revocation slipping between the SELECT
        # and UPDATE. Under the single-writer CLI this is effectively
        # unreachable, but the RuntimeError surfaces it as exit 5 rather
        # than silently succeeding on a no-op UPDATE.
        if cursor.rowcount != 1:
            raise RuntimeError(
                "revoke_api_key: update failed to match any row after SELECT "
                f"succeeded for {key_id!r}"
            )

        log_event(
            conn,
            actor=actor,
            action="revoke_key",
            target_type="api_key",
            target_id=key_id,
            details={"reason": reason},
        )

    _touch_auth_cache_sentinel(conn)

    refreshed = get_key_detail_by_id(conn, key_id, today=today)
    if refreshed is None:
        raise RuntimeError(f"revoke_api_key: row vanished after commit for {key_id!r}")
    return RevokeKeyResult(
        key=refreshed, customer_name=refreshed.customer_name, reason=reason
    )


def mark_key_paid(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    paid_until: str,
    actor: str,
    today: date,
    new_price_monthly: float | None = None,
    audit_extra: dict[str, object] | None = None,
) -> MarkPaidResult | None:
    """Extend ``paid_until`` and auto-reactivate a suspended key (FR15 / FR20).

    Returns ``None`` when the row is missing. Raises ``ValueError`` when the
    row is revoked (terminal) or complimentary (no billing semantics); the
    CLI short-circuits the complimentary path before calling this helper,
    so the ValueError is defense-in-depth for non-CLI callers.

    The UPDATE uses a single CASE expression to flip ``status`` from
    ``suspended`` to ``active`` atomically with the ``paid_until`` write —
    a Python-side branch would open a 1-microsecond race between the SELECT
    and the UPDATE. The ``WHERE id = ? AND status != 'revoked'`` predicate
    mirrors the defense-in-depth pattern from ``update_api_key``.

    ``paid_until`` is caller-validated (YYYY-MM-DD); this helper stores it
    verbatim. Past dates are accepted — retroactive payment logging is a
    legitimate operator workflow (see Story 3.1 AC8).

    ``new_price_monthly`` (Story 5.1 Gap 4) is the optional price override.
    When non-None the same UPDATE writes ``price_monthly``; the audit row
    captures the old/new values under ``price_monthly_changed``. ``audit_extra``
    merges into the audit ``details_json`` payload — used by the Story 5.1
    ``--months`` mode to record ``{"mode": "months", "months": N}``.

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE and audit row either both commit or both roll
    back.
    """
    from compadre.keys.audit import log_event

    reactivated: bool
    with conn:
        row = conn.execute(
            "SELECT status, billing_mode, price_monthly FROM api_key WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "revoked":
            raise ValueError("cannot mark-paid a revoked key")
        if row["billing_mode"] == "complimentary":
            raise ValueError("cannot mark-paid a complimentary key")

        reactivated = row["status"] == "suspended"
        old_price = (
            float(row["price_monthly"]) if row["price_monthly"] is not None else None
        )

        if new_price_monthly is not None:
            cursor = conn.execute(
                "UPDATE api_key SET "
                "paid_until = ?, "
                "price_monthly = ?, "
                "status = CASE WHEN status = 'suspended' THEN 'active' ELSE status END "
                "WHERE id = ? AND status != 'revoked'",
                (paid_until, new_price_monthly, key_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE api_key SET "
                "paid_until = ?, "
                "status = CASE WHEN status = 'suspended' THEN 'active' ELSE status END "
                "WHERE id = ? AND status != 'revoked'",
                (paid_until, key_id),
            )
        if cursor.rowcount != 1:
            # Concurrent writer revoked the row between SELECT and UPDATE;
            # the ``status != 'revoked'`` predicate made our UPDATE a no-op.
            # Matches the defense-in-depth pattern in suspend_key / reactivate_key.
            raise RuntimeError(
                "mark_key_paid: update failed to match any row after SELECT "
                f"succeeded for {key_id!r}"
            )
        details: dict[str, object] = {
            "paid_until": paid_until,
            "reactivated": reactivated,
        }
        if new_price_monthly is not None and (
            old_price is None or float(old_price) != float(new_price_monthly)
        ):
            # Only record the price-change shape when the value actually
            # moved. An operator passing ``--price-monthly 149.0`` against a
            # key already at 149.0 should not produce a misleading
            # ``price_monthly_changed=True`` row in the forensic log.
            details["price_monthly_changed"] = True
            details["old"] = old_price
            details["new"] = float(new_price_monthly)
        if audit_extra:
            # Caller-supplied extras must NOT overwrite structured invariants
            # (``paid_until`` / ``reactivated`` / ``price_monthly_changed`` /
            # ``old`` / ``new``). Reverse the merge priority: extras fill
            # gaps, structured fields always win.
            for key, value in audit_extra.items():
                details.setdefault(key, value)
        log_event(
            conn,
            actor=actor,
            action="mark_paid",
            target_type="api_key",
            target_id=key_id,
            details=details,
        )

    _touch_auth_cache_sentinel(conn)

    refreshed = get_key_detail_by_id(conn, key_id, today=today)
    if refreshed is None:
        raise RuntimeError(f"mark_key_paid: row vanished after commit for {key_id!r}")
    return MarkPaidResult(key=refreshed, reactivated=reactivated)


def suspend_key(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    actor: str,
    today: date,
    reason: str | None = None,
) -> SuspendKeyResult | None:
    """Mark an active ``api_key`` row suspended (Story 3.1 — manual verb).

    Returns ``None`` when the row is missing. Raises ``ValueError`` when the
    row is revoked (terminal) or already suspended (rejects the no-op so the
    operator sees the correction rather than a misleading audit row).

    Writes an audit row with ``action='suspend_key'``. When ``reason`` is
    provided, the audit ``details_json`` carries ``{"reason": <text>}`` (B12
    forensia). Callers must sanitise ``reason`` upstream — empty / NUL /
    multi-line input is the CLI layer's responsibility (mirrors
    ``revoke_api_key --reason`` validators). Cron-sweep callers pass
    ``reason=None`` so manual + cron suspensions render identically in
    ``show --audit-tail`` (operators distinguish via the actor column).

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE and audit row either both commit or both
    roll back.
    """
    from compadre.keys.audit import log_event

    with conn:
        row = conn.execute(
            "SELECT status FROM api_key WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "revoked":
            raise ValueError("cannot suspend a revoked key")
        if row["status"] == "suspended":
            raise ValueError("already suspended")

        cursor = conn.execute(
            "UPDATE api_key SET status = 'suspended' "
            "WHERE id = ? AND status = 'active'",
            (key_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "suspend_key: update failed to match any row after SELECT "
                f"succeeded for {key_id!r}"
            )
        details: dict[str, object] | None = {"reason": reason} if reason else None
        log_event(
            conn,
            actor=actor,
            action="suspend_key",
            target_type="api_key",
            target_id=key_id,
            details=details,
        )

    _touch_auth_cache_sentinel(conn)

    refreshed = get_key_detail_by_id(conn, key_id, today=today)
    if refreshed is None:
        raise RuntimeError(f"suspend_key: row vanished after commit for {key_id!r}")
    return SuspendKeyResult(key=refreshed)


def reactivate_key(
    conn: sqlite3.Connection,
    key_id: str,
    *,
    actor: str,
    today: date,
) -> ReactivateKeyResult | None:
    """Flip a suspended ``api_key`` row back to active (Story 3.1 — FR20 path).

    Returns ``None`` when the row is missing. Raises ``ValueError`` when the
    row is revoked (terminal) or already active.

    ``paid_until`` is explicitly preserved — the UPDATE SET clause does not
    touch it. ``reactivate`` is the "goodwill without payment" path: an
    operator reactivating a suspended key without running ``mark-paid``
    first must not silently extend the billing date (Story 3.1 AC13,
    Journey 3 delinquent-customer flow).

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE and audit row either both commit or both
    roll back.
    """
    from compadre.keys.audit import log_event

    with conn:
        row = conn.execute(
            "SELECT status FROM api_key WHERE id = ?",
            (key_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "revoked":
            raise ValueError("cannot reactivate a revoked key")
        if row["status"] == "active":
            raise ValueError("already active")

        cursor = conn.execute(
            "UPDATE api_key SET status = 'active' "
            "WHERE id = ? AND status = 'suspended'",
            (key_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "reactivate_key: update failed to match any row after SELECT "
                f"succeeded for {key_id!r}"
            )
        log_event(
            conn,
            actor=actor,
            action="reactivate_key",
            target_type="api_key",
            target_id=key_id,
            details=None,
        )

    _touch_auth_cache_sentinel(conn)

    refreshed = get_key_detail_by_id(conn, key_id, today=today)
    if refreshed is None:
        raise RuntimeError(f"reactivate_key: row vanished after commit for {key_id!r}")
    return ReactivateKeyResult(key=refreshed)


def run_suspension_sweep(
    conn: sqlite3.Connection,
    *,
    today: date,
    actor: str = "cron:suspension-sweep",
) -> SuspensionSweepResult:
    """Bulk-suspend manual-mode keys whose ``paid_until`` has elapsed (FR18).

    Single-transaction sweep: one ``UPDATE api_key ... RETURNING`` over the
    ``idx_api_key_sweep`` partial index, one ``log_event`` call per swept
    row, and exactly one ``cron_suspend`` summary audit row — even on a
    zero-row sweep, because FR43 requires the timestamp to be queryable
    regardless of effect.

    Predicate: ``billing_mode='manual' AND status='active'
    AND paid_until IS NOT NULL AND paid_until < today``. Complimentary keys
    and trial-window (NULL ``paid_until``) keys are therefore never touched.
    Strict ``<`` matches FR18 — a key with ``paid_until == today`` is still
    paid through today; the sweep catches it on the next morning.

    ``actor`` defaults to the canonical ``cron:suspension-sweep`` string
    because the audit trail names the *behavior*, not the triggering
    identity (a human running this CLI manually is recovering from a failed
    cron — the audit should read identically). Test fixtures override the
    default to disambiguate seeded rows.

    Callers MUST NOT wrap this in ``with conn:`` — the function owns its
    transaction so the UPDATE, per-key audit rows, and the summary row
    either all commit or all roll back. Requires SQLite >= 3.35 for
    ``UPDATE ... RETURNING`` (Python 3.11+ ships 3.37+).
    """
    from compadre.keys.audit import log_event

    today_iso = today.isoformat()

    with conn:
        cursor = conn.execute(
            "UPDATE api_key "
            "SET status = 'suspended' "
            "WHERE billing_mode = 'manual' "
            "  AND status = 'active' "
            "  AND paid_until IS NOT NULL "
            "  AND paid_until < ? "
            "RETURNING id, paid_until",
            (today_iso,),
        )
        swept_rows = cursor.fetchall()
        swept_key_ids = tuple(row["id"] for row in swept_rows)
        swept_count = len(swept_key_ids)

        # Sample once *after* the UPDATE so ``completed_at`` reflects the
        # moment the state change became effective, then pin the same
        # timestamp on every audit row the sweep writes. Sharing one
        # ``occurred_at`` across per-key + summary rows keeps the sweep
        # coherent as a single atomic event for forensic queries — ordering
        # by (occurred_at, id) matches insertion order instead of
        # interleaving the summary before its own detail rows.
        completed_at = _utcnow_iso()

        for row in swept_rows:
            log_event(
                conn,
                actor=actor,
                action="suspend_key",
                target_type="api_key",
                target_id=row["id"],
                details={"paid_until": row["paid_until"], "cutoff_date": today_iso},
                occurred_at=completed_at,
            )

        log_event(
            conn,
            actor=actor,
            action="cron_suspend",
            target_type="system",
            target_id=None,
            details={"swept_count": swept_count, "cutoff_date": today_iso},
            occurred_at=completed_at,
        )

    _touch_auth_cache_sentinel(conn)

    return SuspensionSweepResult(
        swept_key_ids=swept_key_ids,
        swept_count=swept_count,
        completed_at=completed_at,
        cutoff_date=today_iso,
    )


def get_last_sweep_timestamp(conn: sqlite3.Connection) -> str | None:
    """Return the ISO-UTC timestamp of the most-recent completed suspension sweep.

    Reads ``MAX(occurred_at)`` over ``audit_log`` rows with
    ``action='cron_suspend' AND target_type='system'``. Returns ``None`` on
    a fresh DB where the sweep has never run. Consumed by Story 3.4's
    ``health`` command for FR43.

    The compound predicate (action + target_type) guards against future
    action-name collisions — if some later story introduces a per-key
    ``cron_suspend`` audit row with ``target_type='api_key'``, the summary
    row is still uniquely selected here.
    """
    # ORDER BY (occurred_at, id) DESC instead of MAX(occurred_at) so same-second
    # ties break deterministically by insertion order. ``_utcnow_iso`` has
    # second resolution; two sweeps in the same wall-second (manual + cron
    # collision at 00:30:05) would otherwise produce an arbitrary winner.
    row = conn.execute(
        "SELECT occurred_at AS last_at FROM audit_log "
        "WHERE action = 'cron_suspend' AND target_type = 'system' "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None or row["last_at"] is None:
        return None
    return cast(str, row["last_at"])
