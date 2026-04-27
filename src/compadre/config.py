"""Application configuration via environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_env_file() -> Path:
    """Resolve `.env` relative to the project root, not CWD.

    Walks upward from this module until it finds the directory containing
    `pyproject.toml` (same heuristic used by `uv`, `poetry`, `pipenv`). When
    `compadre-keys` runs from an arbitrary operator CWD, anchoring to the
    project root prevents a stray `.env` from silently overriding the at-rest
    encryption key. When installed as a wheel the pyproject.toml lookup fails
    and no `.env` is loaded — deployments must set env vars explicitly (which
    they already do via compose / systemd).
    """
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor / ".env"
    return Path(".env")


_MODEL_CONFIG = SettingsConfigDict(
    case_sensitive=False,
    env_file=_project_env_file(),
    env_file_encoding="utf-8",
    extra="ignore",
)


class RegistrySettings(BaseSettings):
    """Registry, billing, and operator-contact configuration.

    F33 — split out from ``Settings`` so the ``compadre-keys`` CLI loads
    only the surface it actually consumes. The CLI never touches Whisper /
    model-download paths; routing it through this narrower model means a
    host that has not yet configured the STT side (e.g. an operator
    running ``compadre-keys issue-key`` from a fresh checkout before
    deploying the FastAPI app) does not trip validators or inherit
    defaults that pretend to mean something.

    ``Settings`` extends this class to add the STT-runtime surface — the
    FastAPI app reads everything from a single ``Settings`` instance, so
    no consumer is forced to load both. ``extra="ignore"`` on the model
    config means ``RegistrySettings()`` silently skips any STT-side env
    vars the operator happens to have set; the values are still picked
    up by the FastAPI app via ``get_settings()``.
    """

    model_config = _MODEL_CONFIG

    key_registry_db_path: str = Field(
        default="./data/keys/registry.db",
        min_length=1,
        description=("SQLite key registry path. Created with mode 0600 on first use."),
    )
    key_registry_encryption_key: SecretStr = Field(
        ...,
        description=(
            "Fernet key for encrypting tax IDs at rest. Required — generate "
            'with `python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`.'
        ),
    )
    backup_passphrase: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Passphrase for AES-256 encryption of nightly backup tarballs. "
            "Consumed only by the backup/restore ansible playbooks; NOT read "
            "by the FastAPI app or any compadre-keys CLI verb. Must be at "
            "least 16 characters when set; empty is accepted at service boot "
            "so a deployment that has not yet run bootstrap.yml with "
            "COMPADRE_BACKUP_PASSPHRASE exported can still start the auth path."
        ),
    )
    operator_contact_hint: str = Field(
        default="Contact your Compadre operator for billing.",
        description="Operator contact string shown in CLI error hints.",
    )
    billing_day: int = Field(
        default=5,
        ge=1,
        le=28,
        description="Day-of-month for billing sweeps (1-28 to avoid month-end).",
    )
    billing_timezone: str = Field(
        default="UTC",
        min_length=1,
        description=(
            "IANA timezone used for billing-day calculations and the daily "
            "suspension sweep's day-boundary. Default UTC for operator-neutral "
            "predictability across international deployments; override per "
            "deployment (e.g. America/Sao_Paulo) and communicate the chosen "
            "zone to customers contractually since it governs when paid_until "
            "expires and API keys flip to 401."
        ),
    )

    @field_validator("key_registry_encryption_key")
    @classmethod
    def _validate_fernet_key_shape(cls, v: SecretStr) -> SecretStr:
        """Reject malformed Fernet keys without echoing the raw value.

        Runs on the already-coerced `SecretStr`, so pydantic's error rendering
        masks the input as `**********` — AC5/NFR16 anti-enumeration guarantee
        for the key value itself. The field itself is required (no default),
        so pydantic handles the missing-value case before this runs.
        """
        try:
            Fernet(v.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "KEY_REGISTRY_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc
        return v

    @field_validator("billing_timezone")
    @classmethod
    def _validate_iana_timezone(cls, v: str) -> str:
        """Reject non-IANA timezone strings at config load time.

        Without this, a typo like ``America/Sao-Paulo`` or ``BRT`` silently
        desyncs the runtime BILLING_TIMEZONE from the cron's CRON_TZ — see
        the CLAUDE.md gotcha. Failing fast at boot makes the misconfiguration
        immediately operator-visible.
        """
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"BILLING_TIMEZONE {v!r} is not a valid IANA timezone "
                "(e.g. UTC, America/Sao_Paulo, Europe/Lisbon)"
            ) from exc
        return v

    @field_validator("backup_passphrase")
    @classmethod
    def _validate_backup_passphrase_length(cls, v: SecretStr) -> SecretStr:
        """Accept empty (deferred-bootstrap OK) or length >= 16 chars.

        Empty-at-boot is by design (Story 4.3): the passphrase is consumed
        only by the backup/restore playbooks, not the FastAPI app or any
        CLI verb. Forcing it to be required at boot would make the upgrade
        path chicken-and-egg (service won't start → can't run bootstrap →
        service won't start). The playbooks themselves enforce the length
        check via ``ansible.builtin.assert``.
        """
        secret = v.get_secret_value()
        if secret and len(secret) < 16:
            raise ValueError(
                "BACKUP_PASSPHRASE must be at least 16 characters when set; "
                "leave empty to defer configuration until bootstrap.yml runs "
                "with COMPADRE_BACKUP_PASSPHRASE exported."
            )
        return v


class Settings(RegistrySettings):
    """Full application settings — registry surface plus STT runtime.

    Consumed by the FastAPI app and any code path that needs both the
    registry/billing fields and the engine/port fields.
    The ``compadre-keys`` CLI uses ``RegistrySettings`` directly via
    ``get_registry_settings()``.
    """

    port: int = Field(default=8000, ge=1, le=65535)
    whisper_model_size: str = Field(
        default="large-v3-turbo",
        description="faster-whisper model size (large-v3-turbo only in MVP)",
    )
    whisper_compute_type: str = Field(
        default="int8",
        description="CTranslate2 compute type (int8 on CPU)",
    )
    whisper_cpu_threads: int = Field(
        default=4,
        ge=1,
        description="Number of CPU threads for Whisper inference",
    )
    whisper_language: str | None = Field(
        default="pt",
        description=(
            "Default transcription language (pt = Portuguese; None = auto-detect)."
            " Set the env var to an empty string to enable auto-detect."
        ),
    )

    @field_validator("whisper_language")
    @classmethod
    def _empty_language_to_none(cls, v: str | None) -> str | None:
        """Treat empty string as None so auto-detect works via env var."""
        return None if v == "" else v
    max_upload_size_mb: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum audio upload size in megabytes",
    )
    max_audio_duration_seconds: int = Field(
        default=600,
        ge=1,
        description="Maximum audio duration in seconds (10 minutes)",
    )
    hf_home: str = Field(
        default="./data/cache/huggingface",
        description="HuggingFace Hub cache directory for model auto-download",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached full application settings (STT + registry surface)."""
    return Settings()


@lru_cache(maxsize=1)
def get_registry_settings() -> RegistrySettings:
    """Return cached registry-only settings for ``compadre-keys`` CLI.

    F33 — narrower load surface than ``get_settings()``. Reads the same
    ``.env`` (or process env), but excludes STT-runtime fields entirely,
    so a host without Whisper paths configured can still issue keys,
    run billing reports, and operate the registry.
    """
    return RegistrySettings()
