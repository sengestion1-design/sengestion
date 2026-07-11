"""Subscription handling on the manager side (blocking page)."""
from flask import Blueprint, render_template
from flask_login import login_required, current_user

subscription_bp = Blueprint("subscription", __name__, url_prefix="/subscription")


@subscription_bp.route("/expired")
@login_required
def expired():
    """Page shown when the trial or subscription has expired."""
    return render_template("subscription/expire.html", user=current_user)
