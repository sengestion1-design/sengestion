"""NoSQL data-access service (MongoDB) — competency CP6.

Activity logs are high-volume, semi-structured, relation-free data: an ideal
NoSQL use case. Each user action (login, quote creation, etc.) is stored as a
JSON document in the `activity_logs` collection.

Demonstrates NoSQL CRUD: create (log_action), read (get_recent / count_by_action).

Note: all calls are wrapped so that a MongoDB outage never breaks the app
(logging is best-effort). In production MongoDB is always available.
"""
from datetime import datetime

from flask import current_app

from app.extensions import mongo

COLLECTION = "activity_logs"


def log_action(user_id, action: str, details: dict | None = None):
    """CREATE — store a user action in MongoDB (best-effort)."""
    doc = {
        "user_id": user_id,
        "action": action,
        "details": details or {},
        "created_at": datetime.utcnow(),
    }
    try:
        result = mongo.db[COLLECTION].insert_one(doc)
        return str(result.inserted_id)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB log_action indisponible: %s", exc)
        return None


def get_recent(limit: int = 20, user_id: int | None = None) -> list[dict]:
    """READ — latest actions, newest first (best-effort).

    When user_id is given, only that user's actions are returned — otherwise
    the dashboard would leak every account's activity to every viewer.
    """
    query = {"user_id": user_id} if user_id is not None else {}
    try:
        cursor = mongo.db[COLLECTION].find(query).sort("created_at", -1).limit(limit)
        logs = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            logs.append(doc)
        return logs
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB get_recent indisponible: %s", exc)
        return []


ACTION_LABELS = {
    "login": "Connexion",
    "logout": "Déconnexion",
    "register": "Inscription",
    "create_customer": "Client ajouté",
    "voice_invoice": "Facture créée par dictée vocale",
    "voice_transcribe": "Dictée vocale transcrite",
    "validate_subscription": "Abonnement validé",
    "suspend_account": "Compte suspendu",
}


def describe(log: dict) -> tuple[str, str]:
    """Traduit une entrée de log brute en (libellé d'action, détail lisible)."""
    action = log.get("action", "")
    label = ACTION_LABELS.get(action, action.replace("_", " ").capitalize())
    details = log.get("details") or {}

    if action in ("login", "register") and details.get("email"):
        detail = details["email"]
    elif action == "create_customer" and details.get("name"):
        detail = f"{details['name']} (client #{details.get('customer_id', '?')})"
    elif action == "voice_invoice":
        detail = "Enregistrement traité avec succès" if details.get("ok") else "Échec du traitement"
    elif action == "voice_transcribe":
        detail = "Audio transcrit avec succès" if details.get("ok") else "Échec de la transcription"
    elif not details:
        detail = "—"
    else:
        detail = ", ".join(f"{k} : {v}" for k, v in details.items())

    return label, detail


def count_by_action(action: str) -> int:
    """READ/aggregation — count occurrences of an action (best-effort)."""
    try:
        return mongo.db[COLLECTION].count_documents({"action": action})
    except Exception:  # noqa: BLE001
        return 0
