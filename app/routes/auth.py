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
from app.services.email_service import send_verification_code
from app.utils.validators import validate_password

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        nom = (request.form.get("nom") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        # --- input validation ---
        if not nom or not email or not password:
            flash("Tous les champs sont obligatoires.", "danger")
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
        log_action(user.id, "register", {"email": email})
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
        code = (request.form.get("code") or "").strip()
        if user.check_verification_code(code):
            user.mark_email_verified()
            db.session.commit()
            log_action(user.id, "email_verified", {"email": email})
            session.pop("pending_email", None)
            flash("E-mail vérifié ! Vous pouvez maintenant vous connecter.", "success")
            return redirect(url_for("auth.login"))
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

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            # block login until the email is verified
            if not user.email_verified:
                session["pending_email"] = email
                flash("Veuillez d'abord vérifier votre e-mail.", "warning")
                return redirect(url_for("auth.verify"))

            login_user(user)
            log_action(user.id, "login", {"email": email})
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard.index"))

        flash("E-mail ou mot de passe incorrect.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    log_action(current_user.id, "logout")
    logout_user()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("auth.login"))
