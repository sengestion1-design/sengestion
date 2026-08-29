"""Integration test: full sign-up flow through the real Flask routes.

Unlike the other test files (which call service/util functions directly),
this one drives the application the way a browser would - HTTP requests
through the test client, hitting routing, forms, the ORM, the session and
the email service together - to prove the layers work as a whole.

Flow covered: /register -> /verify (email code) -> /login -> authenticated
access to a protected page.
"""
from app.models.user import User

REGISTER_DATA = {
    "nom": "Awa Diop",
    "email": "awa.diop@example.com",
    "password": "CorrectHorse12!",
    "privacy": "on",
}


def test_register_creates_unverified_user_and_sends_code(client):
    response = client.post("/register", data=REGISTER_DATA, follow_redirects=True)

    assert response.status_code == 200
    user = User.query.filter_by(email=REGISTER_DATA["email"]).first()
    assert user is not None
    assert user.email_verified is False
    assert user.verification_code is not None


def test_login_before_verification_is_rejected(client):
    client.post("/register", data=REGISTER_DATA)

    response = client.post(
        "/login",
        data={"email": REGISTER_DATA["email"], "password": REGISTER_DATA["password"]},
        follow_redirects=True,
    )

    assert b"v\xc3\xa9rifier votre e-mail" in response.data
    user = User.query.filter_by(email=REGISTER_DATA["email"]).first()
    assert user.email_verified is False


def test_full_flow_register_verify_then_login_succeeds(client):
    client.post("/register", data=REGISTER_DATA)
    user = User.query.filter_by(email=REGISTER_DATA["email"]).first()
    code = user.verification_code

    verify_response = client.post(
        "/verify", data={"code": code}, follow_redirects=True
    )
    assert verify_response.status_code == 200
    user = User.query.filter_by(email=REGISTER_DATA["email"]).first()
    assert user.email_verified is True

    login_response = client.post(
        "/login",
        data={"email": REGISTER_DATA["email"], "password": REGISTER_DATA["password"]},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    dashboard_response = client.get("/")
    assert dashboard_response.status_code == 200


def test_wrong_verification_code_is_rejected(client):
    client.post("/register", data=REGISTER_DATA)

    response = client.post("/verify", data={"code": "000000"}, follow_redirects=True)

    assert b"incorrect" in response.data.lower()
    user = User.query.filter_by(email=REGISTER_DATA["email"]).first()
    assert user.email_verified is False
