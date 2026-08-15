"""Messages / Reminders module - sending emails to contacts.

Security (OWASP access control):
- @login_required + @subscription_required on all routes.
- Every message and contact belongs to the manager: systematic filtering
  by user_id = current_user.id (never another manager's data).
- CSRF handled globally by Flask-WTF on all POSTs.
- Server-side validation (recipient with email, subject and body required).
- Sends logged via log_action (MongoDB).
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.customer import Contact, Customer
from app.models.message import Message
from app.services.email_service import send_email_as_manager
from app.services.activity_log_service import log_action
from app.utils.access import subscription_required

messages_bp = Blueprint("messages", __name__, url_prefix="/messages")


# Pre-written message templates ({name} = contact's first/last name, replaced on the JS side).
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


# --------------------------- Helpers ---------------------------

def _owned_contact_or_404(contact_id: int) -> Contact:
    """Return the contact if it belongs to the current manager, or 404."""
    contact = Contact.query.filter_by(
        id=contact_id, user_id=current_user.id
    ).first()
    if contact is None:
        abort(404)
    return contact


def _clean(value, maxlen):
    return (value or "").strip()[:maxlen]


def _resolve_recipient(raw_value: str):
    """Parse a "contact-<id>" / "customer-<id>" select value into
    (contact, customer) - only one of the two is set, owned by the
    current manager. Returns (None, None) if invalid/not found.
    """
    if not raw_value or "-" not in raw_value:
        return None, None
    kind, _, raw_id = raw_value.partition("-")
    if not raw_id.isdigit():
        return None, None
    obj_id = int(raw_id)
    if kind == "contact":
        contact = Contact.query.filter_by(id=obj_id, user_id=current_user.id).first()
        return contact, None
    if kind == "customer":
        customer = Customer.query.filter_by(id=obj_id, user_id=current_user.id).first()
        return None, customer
    return None, None


# --------------------------- Routes ---------------------------

@messages_bp.route("/")
@login_required
@subscription_required
def index():
    """Reminders page: history of every message sent by the manager."""
    msgs = (Message.query
            .filter_by(user_id=current_user.id)
            .order_by(Message.created_at.desc())
            .all())
    # Resolve the recipient name of each message (contact OR customer).
    contact_map = {c.id: c for c in Contact.query.filter_by(user_id=current_user.id).all()}
    customer_map = {c.id: c for c in Customer.query.filter_by(user_id=current_user.id).all()}
    return render_template("messages/index.html",
                           messages=msgs,
                           contact_map=contact_map, customer_map=customer_map)


@messages_bp.route("/new")
@login_required
@subscription_required
def new():
    """Compose form. ?contact_id=… pre-selects a recipient (contact)."""
    contact = None
    cid = request.args.get("contact_id", type=int)
    if cid:
        contact = _owned_contact_or_404(cid)
    contacts = (Contact.query
                .filter_by(user_id=current_user.id)
                .filter(Contact.email.isnot(None), Contact.email != "")
                .order_by(Contact.name)
                .all())
    customers = (Customer.query
                 .filter_by(user_id=current_user.id)
                 .filter(Customer.email.isnot(None), Customer.email != "")
                 .order_by(Customer.name)
                 .all())
    return render_template("messages/compose.html",
                           contact=contact, contacts=contacts, customers=customers,
                           templates=MESSAGE_TEMPLATES)


@messages_bp.route("/send", methods=["POST"])
@login_required
@subscription_required
def send():
    """Send the email to the contact/customer and record the message (sent/failed)."""
    contact, customer = _resolve_recipient(request.form.get("recipient", ""))
    recipient = contact or customer
    if recipient is None or not recipient.email:
        flash("Destinataire invalide ou sans adresse email.", "danger")
        return redirect(url_for("messages.new"))

    subject = _clean(request.form.get("subject"), 255)
    body = (request.form.get("body") or "").strip()
    if not subject or not body:
        flash("L'objet et le message sont obligatoires.", "danger")
        return redirect(url_for("messages.new"))

    # {name} personalization -> first name if available, else last name.
    display_name = getattr(recipient, "first_name", None) or recipient.name
    subject = subject.replace("{name}", display_name)
    body = body.replace("{name}", display_name)

    # Email signature from the company settings (if defined).
    from app.models.settings import CompanySettings
    cfg = CompanySettings.query.filter_by(user_id=current_user.id).first()
    if cfg and cfg.email_signature:
        body = f"{body}\n\n{cfg.email_signature}"

    sender_name = (cfg.email_sender_name if cfg else None) or current_user.name
    ok = send_email_as_manager(cfg, sender_name, recipient.email, subject, body)

    msg = Message(
        user_id=current_user.id,
        contact_id=contact.id if contact else None,
        customer_id=customer.id if customer else None,
        channel="email",
        subject=subject,
        body=body,
        status="sent" if ok else "failed",
        sent_at=datetime.utcnow() if ok else None,
    )
    db.session.add(msg)
    db.session.commit()

    log_action(current_user.id, "send_message",
               {"contact_id": contact.id if contact else None,
                "customer_id": customer.id if customer else None,
                "to": recipient.email, "ok": ok})

    if ok:
        flash(f"Message envoyé à {display_name}.", "success")
    else:
        flash("L'envoi a échoué. Le message a été enregistré comme non envoyé.", "danger")
    return redirect(url_for("messages.index"))


@messages_bp.route("/<int:message_id>/resend", methods=["POST"])
@login_required
@subscription_required
def resend(message_id):
    """Resend an existing message (reminder)."""
    msg = Message.query.filter_by(id=message_id, user_id=current_user.id).first()
    if msg is None:
        abort(404)
    recipient = None
    if msg.contact_id:
        recipient = Contact.query.filter_by(id=msg.contact_id, user_id=current_user.id).first()
    elif msg.customer_id:
        recipient = Customer.query.filter_by(id=msg.customer_id, user_id=current_user.id).first()
    if recipient is None or not recipient.email:
        flash("Ce destinataire n'a plus d'adresse email valide.", "danger")
        return redirect(url_for("messages.index"))

    from app.models.settings import CompanySettings
    cfg = CompanySettings.query.filter_by(user_id=current_user.id).first()
    sender_name = (cfg.email_sender_name if cfg else None) or current_user.name

    ok = send_email_as_manager(cfg, sender_name, recipient.email, msg.subject, msg.body)
    msg.status = "sent" if ok else "failed"
    if ok:
        msg.sent_at = datetime.utcnow()
    db.session.commit()
    log_action(current_user.id, "resend_message",
               {"message_id": msg.id, "to": recipient.email, "ok": ok})
    flash("Message renvoyé." if ok else "Le renvoi a échoué.",
          "success" if ok else "danger")
    return redirect(url_for("messages.index"))
