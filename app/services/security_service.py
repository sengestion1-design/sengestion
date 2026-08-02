"""Anti-brute-force (recommandations OWASP / ANSSI).

Compte les échecs d'authentification dans MongoDB (collection `auth_failures`)
et bloque temporairement après trop de tentatives :

- kind "login" : mots de passe erronés sur /login (5 échecs -> blocage 15 min)
- kind "otp"   : codes à 6 chiffres erronés sur /verify et /reset-password
                 (5 échecs -> blocage 15 min, sinon le code est force-brutable)

Best-effort comme le reste de la couche Mongo : si MongoDB est indisponible
(dev), on n'empêche pas la connexion (fail-open) mais on trace un warning.
En production MongoDB est toujours disponible.
"""
from datetime import datetime, timedelta

from flask import current_app, request

from app.extensions import mongo

COLLECTION = "auth_failures"
MAX_ATTEMPTS = 5
WINDOW_MINUTES = 15


def record_failure(kind: str, identifier: str):
    """Enregistre un échec (horodaté + IP) pour l'identifiant donné."""
    doc = {
        "kind": kind,
        "identifier": identifier,
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "created_at": datetime.utcnow(),
    }
    try:
        mongo.db[COLLECTION].insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB record_failure indisponible: %s", exc)


def is_blocked(kind: str, identifier: str) -> bool:
    """Vrai si l'identifiant a atteint MAX_ATTEMPTS échecs dans la fenêtre."""
    since = datetime.utcnow() - timedelta(minutes=WINDOW_MINUTES)
    try:
        n = mongo.db[COLLECTION].count_documents(
            {"kind": kind, "identifier": identifier, "created_at": {"$gte": since}}
        )
        return n >= MAX_ATTEMPTS
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB is_blocked indisponible: %s", exc)
        return False


def clear_failures(kind: str, identifier: str):
    """Remet le compteur à zéro (après une authentification réussie)."""
    try:
        mongo.db[COLLECTION].delete_many({"kind": kind, "identifier": identifier})
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("MongoDB clear_failures indisponible: %s", exc)


BLOCKED_MESSAGE = (
    "Trop de tentatives. Pour votre sécurité, réessayez dans 15 minutes."
)
