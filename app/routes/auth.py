"""Authentication routes — security (CP3/CP7).

- Passwords hashed (never in clear text)
- CSRF protection (Flask-WTF, enabled globally)
- Input validation
- Email verification by 6-digit code on sign-up
- NoSQL logging of connections (CP6)
"""
from flask import Blueprint, render_template, redirect, url_for, request, flash, session
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db
from app.models.user import User
from app.services.activity_log_service import log_action
from app.services.email_service import send_verification_code, send_password_reset_code
from app.services.security_service import (
    record_failure, is_blocked, clear_failures, BLOCKED_MESSAGE,
)
from app.utils.validators import validate_password

auth_bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Retourne l'URL `next` si elle est locale et sûre, sinon le dashboard.

    Protège contre l'open redirect : on n'accepte qu'un chemin relatif interne
    (commence par un seul '/'), jamais une URL absolue ni un '//host' protocol-relative.
    """
    if not target:
        return url_for("dashboard.index")
    # Rejette les URLs absolues (http://…), protocol-relative (//…) et les backslashs.
    if target.startswith("//") or "://" in target or "\\" in target:
        return url_for("dashboard.index")
    if not target.startswith("/"):
        return url_for("dashboard.index")
    return target


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        nom = (request.form.get("nom") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        privacy_ok = request.form.get("privacy") is not None

        # --- input validation ---
        if not nom or not email or not password:
            flash("Tous les champs sont obligatoires.", "danger")
            return render_template("auth/register.html")
        # consentement RGPD (art. 13) : vérifié aussi côté serveur
        if not privacy_ok:
            flash("Vous devez accepter la politique de confidentialité pour créer un compte.", "danger")
            return render_template("auth/register.html")
        # politique mot de passe CNIL 2022 : 12 car. + complexité
        pwd_error = validate_password(password)
        if pwd_error:
            flash(pwd_error, "danger")
            return render_template("auth/register.html")
        if User.query.filter_by(email=email).first():
            flash("Un compte existe déjà avec cet e-mail.", "danger")
            return render_template("auth/register.html")

        user = User(name=nom, email=email, role="manager", email_verified=False)
        user.set_password(password)   # secure hashing
        user.start_trial()            # 15-day free trial
        code = user.generate_verification_code()   # 6-digit code (valid 15 min)
        db.session.add(user)
        db.session.commit()

        send_verification_code(email, nom, code)   # automatic email
        # trace du consentement RGPD (horodaté par le log)
        log_action(user.id, "register", {"email": email, "privacy_accepted": True})
        # keep the email in session to know which account to verify
        session["pending_email"] = email
        flash("Un code de vérification à 6 chiffres vous a été envoyé par e-mail.", "info")
        return redirect(url_for("auth.verify"))

    return render_template("auth/register.html")


@auth_bp.route("/verify", methods=["GET", "POST"])
def verify():
    """Email verification with the 6-digit code."""
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("auth.register"))

    user = User.query.filter_by(email=email).first()
    if not user:
        session.pop("pending_email", None)
        return redirect(url_for("auth.register"))

    if request.method == "POST":
        # anti-brute-force du code à 6 chiffres : 5 essais max / 15 min
        if is_blocked("otp", email):
            flash(BLOCKED_MESSAGE, "danger")
            return render_template("auth/verify.html", email=email)

        code = (request.form.get("code") or "").strip()
        if user.check_verification_code(code):
            clear_failures("otp", email)
            user.mark_email_verified()
            db.session.commit()
            log_action(user.id, "email_verified", {"email": email})
            session.pop("pending_email", None)
            flash("E-mail vérifié ! Vous pouvez maintenant vous connecter.", "success")
            return redirect(url_for("auth.login"))
        record_failure("otp", email)
        flash("Code incorrect ou expiré.", "danger")

    return render_template("auth/verify.html", email=email)


@auth_bp.route("/resend-code", methods=["POST"])
def resend_code():
    """Send a fresh verification code."""
    email = session.get("pending_email")
    user = User.query.filter_by(email=email).first() if email else None
    if user and not user.email_verified:
        code = user.generate_verification_code()
        db.session.commit()
        send_verification_code(user.email, user.name, code)
        flash("Un nouveau code vous a été envoyé.", "info")
    return redirect(url_for("auth.verify"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        # anti-brute-force : 5 échecs -> blocage temporaire 15 min (OWASP/ANSSI)
        if is_blocked("login", email):
            flash(BLOCKED_MESSAGE, "danger")
            return render_template("auth/login.html")

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            # block login until the email is verified
            if not user.email_verified:
                session["pending_email"] = email
                flash("Veuillez d'abord vérifier votre e-mail.", "warning")
                return redirect(url_for("auth.verify"))

            clear_failures("login", email)
            login_user(user)
            log_action(user.id, "login", {"email": email})
            return redirect(_safe_next(request.args.get("next")))

        # échec : compté pour le blocage + journalisé (traçabilité sécurité)
        record_failure("login", email)
        log_action(user.id if user else None, "login_failed", {"email": email})
        flash("E-mail ou mot de passe incorrect.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Demande de réinitialisation : envoie un code à 6 chiffres par email."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            code = user.generate_verification_code()   # réutilise le circuit 6 chiffres (15 min)
            db.session.commit()
            send_password_reset_code(user.email, user.name, code)
            log_action(user.id, "password_reset_requested", {"email": email})
        # Message identique que le compte existe ou non (anti-énumération d'emails)
        session["reset_email"] = email
        flash("Si un compte existe avec cet e-mail, un code vient de vous être envoyé.", "info")
        return redirect(url_for("auth.reset_password"))

    return render_template("auth/forgot.html")


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    """Saisie du code reçu + nouveau mot de passe."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    email = session.get("reset_email")
    if not email:
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if password != password2:
            flash("Les deux mots de passe ne correspondent pas.", "danger")
            return render_template("auth/reset.html", email=email)
        pwd_error = validate_password(password)
        if pwd_error:
            flash(pwd_error, "danger")
            return render_template("auth/reset.html", email=email)

        # anti-brute-force du code à 6 chiffres : 5 essais max / 15 min
        if is_blocked("otp", email):
            flash(BLOCKED_MESSAGE, "danger")
            return render_template("auth/reset.html", email=email)

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_verification_code(code):
            record_failure("otp", email)
            flash("Code incorrect ou expiré.", "danger")
            return render_template("auth/reset.html", email=email)

        clear_failures("otp", email)
        user.set_password(password)
        user.verification_code = None
        user.verification_expires = None
        db.session.commit()
        log_action(user.id, "password_reset_done", {"email": email})
        session.pop("reset_email", None)
        flash("Mot de passe changé ! Vous pouvez vous connecter.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset.html", email=email)


@auth_bp.route("/logout")
@login_required
def logout():
    log_action(current_user.id, "logout")
    logout_user()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("auth.login"))
