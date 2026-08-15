"""NoSQL data-access service (MongoDB) - competency CP6.

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
    """CREATE - store a user action in MongoDB (best-effort)."""
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
        current_app.logger.warning("MongoDB log_action unavailable: %s", exc)
        return None


def get_recent(limit: int = 20, user_id: int | None = None) -> list[dict]:
    """READ - latest actions, newest first (best-effort).

    When user_id is given, only that user's actions are returned - otherwise
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
        current_app.logger.warning("MongoDB get_recent unavailable: %s", exc)
        return []


ACTION_LABELS = {
    # Compte
    "login": "Connexion",
    "login_failed": "Échec de connexion",
    "logout": "Déconnexion",
    "register": "Inscription",
    "email_verified": "E-mail vérifié",
    "password_reset_requested": "Réinitialisation demandée",
    "password_reset_done": "Mot de passe réinitialisé",
    "update_settings": "Paramètres mis à jour",
    # Contacts et clients
    "create_contact": "Contact ajouté",
    "update_contact": "Contact modifié",
    "delete_contact": "Contact supprimé",
    "promote_contact": "Contact promu prospect",
    "convert_contact": "Contact converti en client",
    "scan_business_card": "Carte de visite scannée (IA)",
    "create_customer": "Client ajouté",
    "update_customer": "Client modifié",
    "delete_customer": "Client supprimé",
    # Devis et factures
    "create_quote": "Devis créé",
    "update_quote": "Devis modifié",
    "delete_quote": "Devis supprimé",
    "voice_quote": "Devis créé par dictée vocale",
    "convert_quote_to_invoice": "Devis converti en facture",
    "public_quote_view": "Devis consulté par le client (lien QR)",
    "public_quote_signed": "Devis signé par le client",
    "create_invoice": "Facture créée",
    "delete_invoice": "Facture supprimée",
    "voice_invoice": "Facture créée par dictée vocale",
    "voice_transcribe": "Dictée vocale transcrite",
    "add_payment": "Paiement encaissé",
    "delete_payment": "Paiement supprimé",
    # Dépenses
    "create_expense": "Dépense enregistrée",
    "update_expense": "Dépense modifiée",
    "delete_expense": "Dépense supprimée",
    "create_expense_category": "Catégorie de dépense créée",
    "delete_expense_category": "Catégorie de dépense supprimée",
    "scan_receipt": "Reçu scanné (IA)",
    # Messages
    "send_message": "Message envoyé",
    "resend_message": "Message renvoyé",
    # Abonnement / administration
    "validate_subscription": "Abonnement validé",
    "suspend_account": "Compte suspendu",
    "submit_payment_proof": "Preuve de paiement envoyée",
    "approve_payment_proof": "Preuve de paiement validée",
    "reject_payment_proof": "Preuve de paiement rejetée",
}


def _fmt_fcfa(value) -> str:
    """53100.00 -> « 53 100 FCFA » (échoue en silence sur valeur non numérique)."""
    try:
        return "{:,.0f}".format(float(value)).replace(",", " ") + " FCFA"
    except (TypeError, ValueError):
        return str(value)


def describe(log: dict) -> tuple[str, str]:
    """Translate a raw log entry into (action label, readable detail)."""
    action = log.get("action", "")
    label = ACTION_LABELS.get(action, action.replace("_", " ").capitalize())
    details = log.get("details") or {}

    if action in ("login", "register", "login_failed") and details.get("email"):
        detail = details["email"]
    elif action == "create_customer" and details.get("name"):
        detail = f"{details['name']} (client #{details.get('customer_id', '?')})"
    elif action == "voice_invoice":
        detail = "Enregistrement traité avec succès" if details.get("ok") else "Échec du traitement"
    elif action == "voice_transcribe":
        detail = "Audio transcrit avec succès" if details.get("ok") else "Échec de la transcription"
    elif action == "public_quote_signed":
        signer = details.get("signer_name", "?")
        detail = f"{details.get('number', '')} - bon pour accord de {signer}".strip(" -")
    elif details.get("number"):
        # documents (devis, factures, paiements) : numéro + montant lisibles
        parts = [str(details["number"])]
        for key in ("amount_incl_tax", "amount"):
            if details.get(key) is not None:
                parts.append(_fmt_fcfa(details[key]))
                break
        if details.get("customer"):
            parts.append(str(details["customer"]))
        detail = " - ".join(parts)
    elif details.get("name"):
        detail = str(details["name"])
    elif details.get("label"):
        parts = [str(details["label"])]
        if details.get("amount") is not None:
            parts.append(_fmt_fcfa(details["amount"]))
        detail = " - ".join(parts)
    elif details.get("email"):
        detail = str(details["email"])
    elif details.get("subject"):
        detail = str(details["subject"])
    elif not details:
        detail = "-"
    else:
        # dernier recours : valeurs seules, sans les noms techniques de champs
        detail = ", ".join(str(v) for v in details.values() if v not in (None, ""))

    return label, detail


def count_by_action(action: str) -> int:
    """READ/aggregation - count occurrences of an action (best-effort)."""
    try:
        return mongo.db[COLLECTION].count_documents({"action": action})
    except Exception:  # noqa: BLE001
        return 0
