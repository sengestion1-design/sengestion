"""Anti-brute-force (OWASP / ANSSI recommendations).

Counts authentication failures in MongoDB (`auth_failures` collection)
and temporarily blocks after too many attempts:

- kind "login" : wrong passwords on /login (5 failures -> 15-min block)
- kind "otp"   : wrong 6-digit codes on /verify and /reset-password
                 (5 failures -> 15-min block, otherwise the code is brute-forceable)

Best-effort like the rest of the Mongo layer: if MongoDB is unavailable
(dev), login is not prevented (fail-open) but a warning is traced.
In production MongoDB is always available.
"""
from datetime import datetime, timedelta

from flask import current_app, request

from app.extensions import mongo

COLLECTION = "auth_failures"
MAX_ATTEMPTS = 5
WINDOW_MINUTES = 15


def record_failure(kind: str, identifier: str):
    """Record a failure (timestamped + IP) for the given identifier."""
    doc = {
        "kind": kind,
        "identifier": identifier,
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "created_at": datetime.utcnow(),
    }
    try:
        mongo.db[COLLECTION].insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB record_failure unavailable: %s", exc)


def is_blocked(kind: str, identifier: str) -> bool:
    """True if the identifier reached MAX_ATTEMPTS failures within the window."""
    since = datetime.utcnow() - timedelta(minutes=WINDOW_MINUTES)
    try:
        n = mongo.db[COLLECTION].count_documents(
            {"kind": kind, "identifier": identifier, "created_at": {"$gte": since}}
        )
        return n >= MAX_ATTEMPTS
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB is_blocked unavailable: %s", exc)
        return False


def clear_failures(kind: str, identifier: str):
    """Reset the counter to zero (after a successful authentication)."""
    try:
        mongo.db[COLLECTION].delete_many({"kind": kind, "identifier": identifier})
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB clear_failures unavailable: %s", exc)


BLOCKED_MESSAGE = (
    "Trop de tentatives. Pour votre sécurité, réessayez dans 15 minutes."
)
