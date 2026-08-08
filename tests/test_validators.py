"""Unit tests for app/utils/validators.py (CNIL 2022 password policy)."""
import pytest

from app.utils.validators import validate_password, PASSWORD_MIN_LENGTH


class TestValidatePassword:
    def test_valid_password_returns_none(self):
        assert validate_password("Str0ng!Passw0rd") is None

    def test_too_short_is_rejected(self):
        msg = validate_password("Sh0rt!")
        assert msg is not None
        assert str(PASSWORD_MIN_LENGTH) in msg

    def test_exactly_min_length_is_accepted_if_valid(self):
        pwd = "Aa1!" + "a" * (PASSWORD_MIN_LENGTH - 4)
        assert len(pwd) == PASSWORD_MIN_LENGTH
        assert validate_password(pwd) is None

    def test_missing_uppercase_is_rejected(self):
        msg = validate_password("lowercase1!only")
        assert msg == "Le mot de passe doit contenir au moins une majuscule."

    def test_missing_lowercase_is_rejected(self):
        msg = validate_password("UPPERCASE1!ONLY")
        assert msg == "Le mot de passe doit contenir au moins une minuscule."

    def test_missing_digit_is_rejected(self):
        msg = validate_password("NoDigitsHere!!")
        assert msg == "Le mot de passe doit contenir au moins un chiffre."

    def test_missing_special_char_is_rejected(self):
        msg = validate_password("NoSpecialChar123")
        assert msg == "Le mot de passe doit contenir au moins un caractère spécial."

    def test_empty_string_is_rejected(self):
        msg = validate_password("")
        assert msg is not None
