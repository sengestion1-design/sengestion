"""Shared pytest fixtures.

`app` boots the real Flask application factory with mail sending suppressed
(Flask-Mail's built-in test mode): no network call is made, but the code path
that builds and "sends" the message still runs for real, and Flask-Mail
records what would have been sent.
"""
import pytest

from app import create_app


@pytest.fixture
def app():
    application = create_app("development")
    application.config.update(
        TESTING=True,
        MAIL_SUPPRESS_SEND=True,
        MAIL_PASSWORD="test-smtp-password",  # forces the real send path, not the [DEV] console fallback
    )
    return application
