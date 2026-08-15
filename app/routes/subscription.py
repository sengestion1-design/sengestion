"""Subscription handling on the manager side (blocking page + payment proof submission).

Security (OWASP access control):
- @login_required on all routes.
- The payment proof form is only usable by the manager whose account is
  actually expired/suspended (server-side check via current_user, not the
  URL) - a manager can only submit proofs tied to their own account.
- CSRF handled globally by Flask-WTF (CSRFProtect).
- Uploaded images are normalized (never trusted raw bytes) before saving,
  same pattern as the receipt scan module.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models.user import User
from app.models.payment_proof import PaymentProof
from app.services.activity_log_service import log_action
from app.services.email_service import send_payment_proof_notification
from app.services.payment_proof_service import normalize_to_jpeg, save_proof

subscription_bp = Blueprint("subscription", __name__, url_prefix="/subscription")

VALID_PLANS = ("monthly", "annual")
VALID_METHODS = ("orange_money", "wave")


@subscription_bp.route("/expired")
@login_required
def expired():
    """Page shown when the trial or subscription has expired."""
    pending = PaymentProof.query.filter_by(
        user_id=current_user.id, status="pending"
    ).order_by(PaymentProof.created_at.desc()).first()
    return render_template("subscription/expire.html", user=current_user, pending=pending)


@subscription_bp.route("/submit-payment", methods=["POST"])
@login_required
def submit_payment():
    """Receive the payment proof (plan, method, transaction number, photo)."""
    plan = request.form.get("plan")
    method = request.form.get("method")
    transaction_number = (request.form.get("transaction_number") or "").strip()
    file = request.files.get("proof")

    errors = []
    if plan not in VALID_PLANS:
        errors.append("Choisissez un plan d'abonnement (mensuel ou annuel).")
    if method not in VALID_METHODS:
        errors.append("Choisissez un moyen de paiement (Orange Money ou Wave).")
    if not transaction_number:
        errors.append("Le numéro de transaction est obligatoire.")
    elif len(transaction_number) > 50:
        errors.append("Le numéro de transaction est trop long.")
    if file is None or not file.filename:
        errors.append("Veuillez joindre une photo de la preuve de paiement.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        return redirect(url_for("subscription.expired"))

    raw = file.read()
    jpeg_bytes = normalize_to_jpeg(raw)
    if jpeg_bytes is None:
        flash("L'image envoyée n'a pas pu être lue. Réessayez avec une photo (JPG, PNG, HEIC).", "danger")
        return redirect(url_for("subscription.expired"))

    image_path = save_proof(jpeg_bytes, current_user.id)
    if image_path is None:
        flash("Une erreur est survenue lors de l'enregistrement de la photo. Réessayez.", "danger")
        return redirect(url_for("subscription.expired"))

    proof = PaymentProof(
        user_id=current_user.id,
        plan=plan,
        method=method,
        transaction_number=transaction_number,
        image_path=image_path,
    )
    db.session.add(proof)
    db.session.commit()

    log_action(current_user.id, "submit_payment_proof", {
        "proof_id": proof.id, "plan": plan, "method": method,
    })

    # Notify every admin (best-effort: a failed email never blocks the submission).
    admins = User.query.filter_by(role="admin").all()
    for admin in admins:
        try:
            send_payment_proof_notification(
                admin_email=admin.email,
                admin_name=admin.name,
                manager_name=current_user.name,
                manager_company=current_user.company,
                plan=plan,
                method=method,
                transaction_number=transaction_number,
                review_url=url_for("admin.index", _external=True) + "#payment-proofs",
            )
        except Exception:
            pass

    flash(
        "Votre preuve de paiement a été envoyée. Un administrateur va la vérifier "
        "et activer votre abonnement sous peu.",
        "success",
    )
    return redirect(url_for("subscription.expired"))
