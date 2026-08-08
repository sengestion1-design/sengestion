"""Integration tests for app/services/email_service.py.

Unlike the pure unit tests, these exercise the real send path (Flask app
context, Flask-Mail, HTML template rendering) with MAIL_SUPPRESS_SEND=True:
no email actually leaves the machine, but everything up to the SMTP call
runs for real and is inspected via Flask-Mail's `mail.record_messages()`.
"""
from app.extensions import mail
from app.services.email_service import send_verification_code, send_password_reset_code


class TestSendVerificationCode:
    def test_returns_true_on_success(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                ok = send_verification_code("client@example.com", "Awa", "654321")
        assert ok is True
        assert len(outbox) == 1

    def test_email_is_sent_to_the_right_recipient(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_verification_code("client@example.com", "Awa", "654321")
        assert outbox[0].recipients == ["client@example.com"]

    def test_subject_mentions_verification(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_verification_code("client@example.com", "Awa", "654321")
        assert "vérification" in outbox[0].subject.lower()

    def test_code_appears_in_plain_text_body(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_verification_code("client@example.com", "Awa", "654321")
        assert "654321" in outbox[0].body

    def test_code_appears_in_html_body(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_verification_code("client@example.com", "Awa", "654321")
        assert "654321" in outbox[0].html

    def test_recipient_name_appears_in_body(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_verification_code("client@example.com", "Awa Diop", "111222")
        assert "Awa Diop" in outbox[0].body
        assert "Awa Diop" in outbox[0].html

    def test_html_uses_brand_colors_only(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_verification_code("client@example.com", "Awa", "111222")
        html = outbox[0].html.lower()
        # the 3 official SenGestion brand colors
        assert "#021a3d" in html  # navy
        assert "#f2b10e" in html  # gold
        assert "#e8e7a2" in html  # pale yellow


class TestSendPasswordResetCode:
    def test_returns_true_on_success(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                ok = send_password_reset_code("client@example.com", "Awa", "999888")
        assert ok is True
        assert len(outbox) == 1

    def test_subject_mentions_reset(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_password_reset_code("client@example.com", "Awa", "999888")
        assert "réinitialisation" in outbox[0].subject.lower()

    def test_code_appears_in_both_bodies(self, app):
        with app.app_context():
            with mail.record_messages() as outbox:
                send_password_reset_code("client@example.com", "Awa", "999888")
        assert "999888" in outbox[0].body
        assert "999888" in outbox[0].html
