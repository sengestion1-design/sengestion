"""Module Messages / Relances — envoi d'emails aux contacts.

Sécurité (OWASP contrôle d'accès) :
- @login_required + @subscription_required sur toutes les routes.
- Chaque message et chaque contact appartient au gérant : filtrage systématique
  par user_id = current_user.id (jamais les données d'un autre gérant).
- CSRF assuré globalement par Flask-WTF sur tous les POST.
- Validation serveur (destinataire avec email, objet et corps requis).
- Envois journalisés via log_action (MongoDB).
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.customer import Contact
from app.models.message import Message
from app.services.email_service import send_email
from app.services.activity_log_service import log_action
from app.utils.access import subscription_required

messages_bp = Blueprint("messages", __name__, url_prefix="/messages")


# Modèles de message pré-écrits ({name} = prénom/nom du contact, remplacé côté JS).
MESSAGE_TEMPLATES = {
    "contact": {
        "label": "Prise de contact",
        "subject": "Ravi d'avoir fait votre connaissance",
        "body": (
            "Bonjour {name},\n\n"
            "Ravi d'avoir échangé avec vous. Je me permets de vous recontacter "
            "pour poursuivre notre discussion.\n\n"
            "N'hésitez pas à me dire comment je peux vous être utile.\n\n"
            "Bien cordialement,"
        ),
    },
    "relance_devis": {
        "label": "Relance devis",
        "subject": "Suivi de notre devis",
        "body": (
            "Bonjour {name},\n\n"
            "Je reviens vers vous concernant le devis que je vous ai transmis. "
            "Avez-vous eu l'occasion de l'examiner ?\n\n"
            "Je reste à votre disposition pour toute question ou ajustement.\n\n"
            "Bien cordialement,"
        ),
    },
    "remerciement": {
        "label": "Remerciement",
        "subject": "Merci pour votre confiance",
        "body": (
            "Bonjour {name},\n\n"
            "Je tenais à vous remercier pour votre confiance. "
            "Ce fut un plaisir de travailler avec vous.\n\n"
            "Au plaisir de vous accompagner à nouveau.\n\n"
            "Bien cordialement,"
        ),
    },
}


# ─────────────────────────── Helpers ───────────────────────────

def _owned_contact_or_404(contact_id: int) -> Contact:
    """Retourne le contact s'il appartient au gérant courant, sinon 404."""
    contact = Contact.query.filter_by(
        id=contact_id, user_id=current_user.id
    ).first()
    if contact is None:
        abort(404)
    return contact


def _clean(value, maxlen):
    return (value or "").strip()[:maxlen]


# ─────────────────────────── Routes ───────────────────────────

@messages_bp.route("/")
@login_required
@subscription_required
def index():
    """Page Relances : historique de tous les messages envoyés par le gérant."""
    msgs = (Message.query
            .filter_by(user_id=current_user.id)
            .order_by(Message.created_at.desc())
            .all())
    # Contacts (avec email) pour composer un nouveau message.
    contacts = (Contact.query
                .filter_by(user_id=current_user.id)
                .filter(Contact.email.isnot(None), Contact.email != "")
                .order_by(Contact.name)
                .all())
    # Résoudre le nom du destinataire de chaque message.
    contact_map = {c.id: c for c in Contact.query.filter_by(user_id=current_user.id).all()}
    return render_template("messages/index.html",
                           messages=msgs, contacts=contacts,
                           contact_map=contact_map)


@messages_bp.route("/new")
@login_required
@subscription_required
def new():
    """Formulaire de composition. ?contact_id=… pré-sélectionne un destinataire."""
    contact = None
    cid = request.args.get("contact_id", type=int)
    if cid:
        contact = _owned_contact_or_404(cid)
    contacts = (Contact.query
                .filter_by(user_id=current_user.id)
                .filter(Contact.email.isnot(None), Contact.email != "")
                .order_by(Contact.name)
                .all())
    return render_template("messages/compose.html",
                           contact=contact, contacts=contacts,
                           templates=MESSAGE_TEMPLATES)


@messages_bp.route("/send", methods=["POST"])
@login_required
@subscription_required
def send():
    """Envoie l'email au contact et enregistre le message (envoyé/échec)."""
    contact_id = request.form.get("contact_id", type=int)
    contact = _owned_contact_or_404(contact_id) if contact_id else None
    if contact is None or not contact.email:
        flash("Destinataire invalide ou sans adresse email.", "danger")
        return redirect(url_for("messages.new"))

    subject = _clean(request.form.get("subject"), 255)
    body = (request.form.get("body") or "").strip()
    if not subject or not body:
        flash("L'objet et le message sont obligatoires.", "danger")
        return redirect(url_for("messages.new", contact_id=contact.id))

    # Personnalisation {name} -> prénom si dispo, sinon nom.
    display_name = contact.first_name or contact.name
    subject = subject.replace("{name}", display_name)
    body = body.replace("{name}", display_name)

    # Signature email des paramètres de l'entreprise (si définie).
    from app.models.settings import CompanySettings
    cfg = CompanySettings.query.filter_by(user_id=current_user.id).first()
    if cfg and cfg.email_signature:
        body = f"{body}\n\n{cfg.email_signature}"

    ok = send_email(contact.email, subject, body)

    msg = Message(
        user_id=current_user.id,
        contact_id=contact.id,
        channel="email",
        subject=subject,
        body=body,
        status="sent" if ok else "failed",
        sent_at=datetime.utcnow() if ok else None,
    )
    db.session.add(msg)
    db.session.commit()

    log_action(current_user.id, "send_message",
               {"contact_id": contact.id, "to": contact.email, "ok": ok})

    if ok:
        flash(f"Message envoyé à {display_name}.", "success")
    else:
        flash("L'envoi a échoué. Le message a été enregistré comme non envoyé.", "danger")
    return redirect(url_for("messages.index"))


@messages_bp.route("/<int:message_id>/resend", methods=["POST"])
@login_required
@subscription_required
def resend(message_id):
    """Renvoie un message existant (relance)."""
    msg = Message.query.filter_by(id=message_id, user_id=current_user.id).first()
    if msg is None:
        abort(404)
    contact = Contact.query.filter_by(id=msg.contact_id, user_id=current_user.id).first()
    if contact is None or not contact.email:
        flash("Ce contact n'a plus d'adresse email valide.", "danger")
        return redirect(url_for("messages.index"))

    ok = send_email(contact.email, msg.subject, msg.body)
    msg.status = "sent" if ok else "failed"
    if ok:
        msg.sent_at = datetime.utcnow()
    db.session.commit()
    log_action(current_user.id, "resend_message",
               {"message_id": msg.id, "to": contact.email, "ok": ok})
    flash("Message renvoyé." if ok else "Le renvoi a échoué.",
          "success" if ok else "danger")
    return redirect(url_for("messages.index"))
