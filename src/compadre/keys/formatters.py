"""Shared humanizers used across the keys CLI and health collector.

Sits below ``cli.output`` and ``keys.health`` in the import graph so both
can consume the same renderer without creating a cycle. Kept minimal:
only helpers that are genuinely used in more than one module live here.
"""

from __future__ import annotations

from compadre.keys.models import TaxIdKind


def format_tax_id_display(canonical: str, kind: TaxIdKind) -> str:
    """Return the human-readable form of a canonical CPF / CNPJ.

    Examples:
        ``format_tax_id_display("12345678909", "cpf")`` → ``"123.456.789-09"``
        ``format_tax_id_display("12345678000195", "cnpj")`` → ``"12.345.678/0001-95"``
        ``format_tax_id_display("12ABC345XY0189", "cnpj")`` → ``"12.ABC.345/XY01-89"``

    Input MUST be the canonical stripped+uppercased form as produced by
    ``strip_tax_id`` and persisted in ``tax_id_encrypted``. Invalid
    lengths raise ``ValueError`` — export callers should have already
    validated the length on decrypt (11 for CPF, 14 for CNPJ); a surprise
    here would indicate ciphertext / schema drift, not operator input.
    """
    if kind == "cpf":
        if len(canonical) != 11:
            raise ValueError(f"CPF must be 11 chars, got {len(canonical)}")
        return f"{canonical[0:3]}.{canonical[3:6]}.{canonical[6:9]}-{canonical[9:11]}"
    # CNPJ: 14 chars, numeric or alphanumeric (RFB IN 2.229/2024).
    if len(canonical) != 14:
        raise ValueError(f"CNPJ must be 14 chars, got {len(canonical)}")
    return (
        f"{canonical[0:2]}.{canonical[2:5]}.{canonical[5:8]}/"
        f"{canonical[8:12]}-{canonical[12:14]}"
    )


def format_seconds_ago(seconds: int) -> str:
    """Render an age in seconds as a compact human duration.

    Clamps negatives to zero so a small clock skew never produces
    ``-1s ago``. Output shape: ``Ns ago``, ``Nm ago``, ``Nh MMm ago``,
    ``Nd HHh ago``.
    """
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {remaining_minutes:02d}m ago"
    days, remaining_hours = divmod(hours, 24)
    return f"{days}d {remaining_hours:02d}h ago"
