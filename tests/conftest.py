"""Shared pytest fixtures.

`app` boots the real Flask application factory with mail sending suppressed
(Flask-Mail's built-in test mode): no network call is made, but the code path
that builds and "sends" the message still runs for real, and Flask-Mail
records what would have been sent.

`db_app` additionally swaps MySQL for an in-memory SQLite database, so the
integration tests exercise the real Flask routes and ORM models without
requiring a live MySQL server.

IMPORTANT: Flask-Mail and Flask-SQLAlchemy both read their settings from
app.config *inside* create_app() (mail.init_app / db.init_app), and cache
them (Flask-Mail builds its Connection settings, SQLAlchemy binds its
engine). Updating application.config *after* create_app() has already run
has no effect on either - the class attributes on config.settings.Config
must be patched before create_app() is called.
"""
import pytest

from app import create_app
from app.extensions import db as _db
from config.settings import DevelopmentConfig


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(DevelopmentConfig, "MAIL_SUPPRESS_SEND", True, raising=False)
    monkeypatch.setattr(DevelopmentConfig, "MAIL_PASSWORD", "test-smtp-password")

    application = create_app("development")
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def db_app(monkeypatch):
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    monkeypatch.setattr(DevelopmentConfig, "MAIL_SUPPRESS_SEND", True, raising=False)
    monkeypatch.setattr(DevelopmentConfig, "MAIL_PASSWORD", "test-smtp-password")
    monkeypatch.setattr(DevelopmentConfig, "WTF_CSRF_ENABLED", False)  # CSRF is covered separately; disabled here to POST forms directly

    application = create_app("development")
    application.config.update(TESTING=True)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(db_app):
    return db_app.test_client()
