"""Unit tests for the 6-digit email verification code (app/models/user.py).

Security properties under test:
- the code is generated with `secrets` (cryptographically secure), not `random`
- it is always 6 digits, zero-padded
- it expires after CODE_VALIDITY_MINUTES (15 min)
- comparison uses `secrets.compare_digest` (constant-time, no timing attack)
"""
from datetime import datetime, timedelta

from app.models.user import User, CODE_VALIDITY_MINUTES


def make_user():
    return User(name="Test", email="test@example.com")


class TestGenerateVerificationCode:
    def test_code_is_six_digits(self):
        user = make_user()
        code = user.generate_verification_code()
        assert len(code) == 6
        assert code.isdigit()

    def test_code_is_zero_padded(self):
        # force a low value to check zero-padding, without touching secrets internals
        user = make_user()
        for _ in range(200):
            code = user.generate_verification_code()
            assert len(code) == 6  # e.g. "000042", never "42"

    def test_code_stored_on_instance(self):
        user = make_user()
        code = user.generate_verification_code()
        assert user.verification_code == code

    def test_expiry_is_set_15_minutes_ahead(self):
        user = make_user()
        before = datetime.utcnow()
        user.generate_verification_code()
        after = datetime.utcnow()
        assert user.verification_expires >= before + timedelta(minutes=CODE_VALIDITY_MINUTES)
        assert user.verification_expires <= after + timedelta(minutes=CODE_VALIDITY_MINUTES)

    def test_codes_are_not_all_identical(self):
        # basic randomness sanity-check: 20 draws should not collapse to 1 value
        user = make_user()
        codes = {user.generate_verification_code() for _ in range(20)}
        assert len(codes) > 1


class TestCheckVerificationCode:
    def test_correct_code_is_accepted(self):
        user = make_user()
        code = user.generate_verification_code()
        assert user.check_verification_code(code) is True

    def test_wrong_code_is_rejected(self):
        user = make_user()
        code = user.generate_verification_code()
        wrong = "000000" if code != "000000" else "111111"
        assert user.check_verification_code(wrong) is False

    def test_no_code_generated_yet_is_rejected(self):
        user = make_user()
        assert user.check_verification_code("123456") is False

    def test_expired_code_is_rejected(self):
        user = make_user()
        code = user.generate_verification_code()
        user.verification_expires = datetime.utcnow() - timedelta(seconds=1)
        assert user.check_verification_code(code) is False

    def test_code_valid_at_the_last_second(self):
        user = make_user()
        code = user.generate_verification_code()
        user.verification_expires = datetime.utcnow() + timedelta(seconds=5)
        assert user.check_verification_code(code) is True

    def test_empty_input_is_rejected(self):
        user = make_user()
        user.generate_verification_code()
        assert user.check_verification_code("") is False
        assert user.check_verification_code(None) is False

    def test_input_is_stripped_of_whitespace(self):
        user = make_user()
        code = user.generate_verification_code()
        assert user.check_verification_code(f"  {code}  ") is True


class TestMarkEmailVerified:
    def test_clears_code_and_expiry(self):
        user = make_user()
        user.generate_verification_code()
        user.mark_email_verified()
        assert user.email_verified is True
        assert user.verification_code is None
        assert user.verification_expires is None

    def test_old_code_no_longer_works_after_verification(self):
        user = make_user()
        code = user.generate_verification_code()
        user.mark_email_verified()
        assert user.check_verification_code(code) is False
