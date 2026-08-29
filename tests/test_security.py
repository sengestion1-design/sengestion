"""Security tests (OWASP-driven).

- Anti-open-redirect: `_safe_next` (app/routes/auth.py) must never send a
  logged-in user to an external site via the `?next=` parameter.
- Anti-brute-force: `security_service` (app/services/security_service.py)
  must block an identifier after MAX_ATTEMPTS failures within the time
  window, and unblock it after `clear_failures`.
"""
import mongomock
import pytest

from app.routes.auth import _safe_next
from app.services import security_service


# --- Anti-open-redirect (OWASP A01 / A10) ---------------------------------

class TestSafeNext:
    def test_none_defaults_to_dashboard(self, app):
        with app.test_request_context():
            assert _safe_next(None) == "/"

    def test_empty_string_defaults_to_dashboard(self, app):
        with app.test_request_context():
            assert _safe_next("") == "/"

    def test_local_relative_path_is_accepted(self, app):
        with app.test_request_context():
            assert _safe_next("/quotes/42") == "/quotes/42"

    def test_absolute_external_url_is_rejected(self, app):
        with app.test_request_context():
            assert _safe_next("http://evil.example/phishing") == "/"

    def test_protocol_relative_url_is_rejected(self, app):
        with app.test_request_context():
            assert _safe_next("//evil.example/phishing") == "/"

    def test_backslash_variant_is_rejected(self, app):
        # Browsers can normalize "/\evil.example" to "//evil.example".
        with app.test_request_context():
            assert _safe_next("/\\evil.example") == "/"

    def test_path_without_leading_slash_is_rejected(self, app):
        with app.test_request_context():
            assert _safe_next("evil.example") == "/"


# --- Anti-brute-force (OWASP / ANSSI) -------------------------------------

@pytest.fixture(autouse=True)
def fake_mongo(app, monkeypatch):
    """Replace the real MongoDB client with an in-memory mock for these tests.

    Must depend on `app`: the application factory calls `mongo.init_app()`,
    which points `security_service.mongo.db` back at a real (unreachable)
    MongoDB client. Patching only takes effect once `app` already ran.
    """
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(security_service.mongo, "db", fake_client["sengestion_nosql"])
    yield


class TestAntiBruteForce:
    def test_not_blocked_before_max_attempts(self, app):
        with app.test_request_context():
            for _ in range(security_service.MAX_ATTEMPTS - 1):
                security_service.record_failure("login", "victim@example.com")
            assert security_service.is_blocked("login", "victim@example.com") is False

    def test_blocked_after_max_attempts(self, app):
        with app.test_request_context():
            for _ in range(security_service.MAX_ATTEMPTS):
                security_service.record_failure("login", "victim@example.com")
            assert security_service.is_blocked("login", "victim@example.com") is True

    def test_clear_failures_unblocks(self, app):
        with app.test_request_context():
            for _ in range(security_service.MAX_ATTEMPTS):
                security_service.record_failure("login", "victim@example.com")
            security_service.clear_failures("login", "victim@example.com")
            assert security_service.is_blocked("login", "victim@example.com") is False

    def test_attempts_are_isolated_per_identifier(self, app):
        with app.test_request_context():
            for _ in range(security_service.MAX_ATTEMPTS):
                security_service.record_failure("login", "victim@example.com")
            assert security_service.is_blocked("login", "someone-else@example.com") is False

    def test_attempts_are_isolated_per_kind(self, app):
        # A user failing the OTP check should not lock out their password login.
        with app.test_request_context():
            for _ in range(security_service.MAX_ATTEMPTS):
                security_service.record_failure("otp", "victim@example.com")
            assert security_service.is_blocked("login", "victim@example.com") is False
