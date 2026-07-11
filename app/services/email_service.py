"""Email service (Gmail SMTP) — sends the verification code on sign-up."""
from flask import current_app
from flask_mail import Message

from app.extensions import mail


def send_verification_code(to_email: str, name: str, code: str) -> bool:
    """Send the 6-digit verification code by email.

    Returns True on success. In development, if no SMTP password is set,
    the code is printed to the console so the flow can be tested offline.
    """
    subject = "SenGestion — Code de vérification"
    body = (
        f"Bonjour {name},\n\n"
        f"Merci de votre inscription sur SenGestion.\n"
        f"Votre code de vérification est : {code}\n\n"
        f"Ce code est valable 15 minutes.\n"
        f"Si vous n'êtes pas à l'origine de cette inscription, ignorez cet email.\n\n"
        f"— L'équipe SenGestion"
    )

    # Développement : pas de mot de passe SMTP -> on affiche le code en console
    if not current_app.config.get("MAIL_PASSWORD"):
        current_app.logger.warning(
            "[DEV] SMTP non configuré — code de vérification pour %s : %s", to_email, code
        )
        print(f"\n[DEV] Code de vérification pour {to_email} : {code}\n")
        return True

    try:
        msg = Message(subject=subject, recipients=[to_email], body=body)
        mail.send(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("Échec envoi email à %s : %s", to_email, exc)
        return False
