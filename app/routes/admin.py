"""Administrator area - manage managers' subscriptions.

Restricted to the 'admin' role. Lists all accounts, validates a yearly
subscription, suspends or reactivates.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models.user import User
from app.utils.access import admin_required
from app.services.activity_log_service import log_action

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/")
@login_required
@admin_required
def index():
    managers = User.query.filter_by(role="manager").order_by(User.created_at.desc()).all()
    # refresh statuses (trial/active -> expired if past due)
    for m in managers:
        m.refresh_status()
    db.session.commit()

    stats = {
        "total": len(managers),
        "trial": sum(1 for m in managers if m.status == "trial"),
        "active": sum(1 for m in managers if m.status == "active"),
        "expired": sum(1 for m in managers if m.status == "expired"),
        "suspended": sum(1 for m in managers if m.status == "suspended"),
    }
    return render_template("admin/index.html", managers=managers, stats=stats)


@admin_bp.route("/validate/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def validate(user_id):
    user = User.query.filter_by(id=user_id, role="manager").first_or_404()
    user.validate_subscription(admin_id=current_user.id)
    db.session.commit()
    log_action(current_user.id, "validate_subscription", {"manager_id": user_id})
    flash(f"Abonnement de {user.name} validé pour 1 an.", "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/suspend/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def suspend(user_id):
    user = User.query.filter_by(id=user_id, role="manager").first_or_404()
    user.suspend()
    db.session.commit()
    log_action(current_user.id, "suspend_account", {"manager_id": user_id})
    flash(f"Compte de {user.name} suspendu.", "info")
    return redirect(url_for("admin.index"))
