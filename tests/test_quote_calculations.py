"""Unit tests for the pure calculation helpers of app/routes/quotes.py.

These functions have no database dependency: they can be tested in isolation,
without a Flask app context or a database connection.
"""
from decimal import Decimal

import pytest

from app.routes.quotes import _parse_decimal, _totals, _fmt, _n2w, _amount_words


# ------------------------------------------------------------
# _parse_decimal
# ------------------------------------------------------------
class TestParseDecimal:
    def test_simple_integer(self):
        assert _parse_decimal("100") == Decimal("100.00")

    def test_french_comma_decimal(self):
        assert _parse_decimal("12,50") == Decimal("12.50")

    def test_dot_decimal(self):
        assert _parse_decimal("12.5") == Decimal("12.50")

    def test_thousands_with_spaces(self):
        assert _parse_decimal("1 000,50") == Decimal("1000.50")

    def test_none_input(self):
        assert _parse_decimal(None) is None

    def test_empty_string(self):
        assert _parse_decimal("") is None

    def test_blank_string(self):
        assert _parse_decimal("   ") is None

    def test_negative_rejected(self):
        assert _parse_decimal("-5") is None

    def test_zero_rejected_by_default(self):
        assert _parse_decimal("0") is None

    def test_zero_allowed_when_flagged(self):
        assert _parse_decimal("0", allow_zero=True) == Decimal("0.00")

    def test_non_numeric_rejected(self):
        assert _parse_decimal("abc") is None

    def test_rounds_to_two_decimals(self):
        assert _parse_decimal("12.999") == Decimal("13.00")


# ------------------------------------------------------------
# _totals (VAT 18%)
# ------------------------------------------------------------
class TestTotals:
    def test_empty_list(self):
        excl, incl = _totals([])
        assert excl == Decimal("0.00")
        assert incl == Decimal("0.00")

    def test_single_line(self):
        items = [{"amount": Decimal("100.00")}]
        excl, incl = _totals(items)
        assert excl == Decimal("100.00")
        assert incl == Decimal("118.00")

    def test_multiple_lines(self):
        items = [
            {"amount": Decimal("150000.00")},
            {"amount": Decimal("27000.00")},
        ]
        excl, incl = _totals(items)
        assert excl == Decimal("177000.00")
        assert incl == Decimal("208860.00")

    def test_rounding_is_applied_on_final_result(self):
        items = [{"amount": Decimal("10.005")}]
        excl, incl = _totals(items)
        # excl is rounded to 2 decimals before VAT is applied
        assert excl == Decimal("10.01") or excl == Decimal("10.00")
        assert incl == (excl * Decimal("1.18")).quantize(Decimal("0.01"))


# ------------------------------------------------------------
# _fmt (FCFA display formatting)
# ------------------------------------------------------------
class TestFmt:
    def test_formats_thousands_with_space(self):
        assert _fmt(1234567) == "1 234 567"

    def test_zero(self):
        assert _fmt(0) == "0"

    def test_none_falls_back_to_zero(self):
        assert _fmt(None) == "0"

    def test_invalid_value_falls_back_to_zero(self):
        assert _fmt("abc") == "0"

    def test_decimal_has_no_decimals_in_output(self):
        assert _fmt(Decimal("1500.99")) == "1 501"  # rounded via format spec


# ------------------------------------------------------------
# _n2w (integer to French words)
# ------------------------------------------------------------
class TestNumberToWords:
    @pytest.mark.parametrize("n,expected", [
        (0, "zéro"),
        (1, "un"),
        (19, "dix-neuf"),
        (20, "vingt"),
        (21, "vingt-et-un"),
        (69, "soixante-neuf"),
        (71, "soixante-et-onze"),
        (80, "quatre-vingts"),
        (91, "quatre-vingt-onze"),
        (100, "cent"),
        (101, "cent-un"),
        (200, "deux-cents"),
        (1000, "mille"),
        (2000, "deux-mille"),
        (1_000_000, "un-million"),
        (2_000_000, "deux-millions"),
    ])
    def test_known_values(self, n, expected):
        assert _n2w(n) == expected


# ------------------------------------------------------------
# _amount_words (FCFA amount spelled out)
# ------------------------------------------------------------
class TestAmountWords:
    def test_capitalizes_first_letter(self):
        assert _amount_words(1500) == "Mille-cinq-cents francs CFA"

    def test_none_defaults_to_zero(self):
        assert _amount_words(None) == "Zéro francs CFA"

    def test_invalid_value_defaults_to_zero(self):
        assert _amount_words("abc") == "Zéro francs CFA"

    def test_truncates_decimals(self):
        assert _amount_words(Decimal("1500.99")) == "Mille-cinq-cents francs CFA"
