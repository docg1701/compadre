"""Tests for compadre.keys.formatters — shared display humanizers."""

from __future__ import annotations

import pytest

from compadre.keys.formatters import format_tax_id_display


class TestFormatTaxIdDisplay:
    """Story 4.1 — canonical → formatted CPF / CNPJ for LGPD export."""

    def test_cpf_eleven_digits(self) -> None:
        assert format_tax_id_display("12345678909", "cpf") == "123.456.789-09"

    def test_cpf_with_leading_zero(self) -> None:
        assert format_tax_id_display("01234567890", "cpf") == "012.345.678-90"

    def test_cnpj_numeric(self) -> None:
        assert format_tax_id_display("12345678000195", "cnpj") == "12.345.678/0001-95"

    def test_cnpj_alphanumeric(self) -> None:
        """RFB IN 2.229/2024 alphanumeric CNPJ (jul/2026)."""
        assert format_tax_id_display("12ABC345XY0189", "cnpj") == "12.ABC.345/XY01-89"

    @pytest.mark.parametrize("bad", ["1234567890", "123456789091", ""])
    def test_cpf_rejects_wrong_length(self, bad: str) -> None:
        with pytest.raises(ValueError, match="CPF must be 11 chars"):
            format_tax_id_display(bad, "cpf")

    @pytest.mark.parametrize("bad", ["1234567890019", "123456780001955", ""])
    def test_cnpj_rejects_wrong_length(self, bad: str) -> None:
        with pytest.raises(ValueError, match="CNPJ must be 14 chars"):
            format_tax_id_display(bad, "cnpj")
