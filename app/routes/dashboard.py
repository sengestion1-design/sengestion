"""Dashboard route (protected page + subscription required)."""
from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

from app.extensions import db
from app.models.customer import Customer
from app.models.quote import Quote, Invoice, Payment
from app.models.expense import Expense
from app.services.activity_log_service import get_recent, describe
from app.utils.access import subscription_required

dashboard_bp = Blueprint("dashboard", __name__)


def _build_stats(user_id):
    """KPIs displayed on the dashboard (SQL aggregates)."""
    customers = db.session.query(Customer).filter_by(user_id=user_id).count()
    quotes = db.session.query(Quote).filter_by(user_id=user_id).count()
    invoices = db.session.query(Invoice).filter_by(user_id=user_id).count()

    encaisse = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)) \
        .join(Invoice, Payment.invoice_id == Invoice.id) \
        .filter(Invoice.user_id == user_id).scalar() or 0
    depenses = db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0)) \
        .filter(Expense.user_id == user_id).scalar() or 0

    return {
        "customers": customers,
        "quotes": quotes,
        "invoices": invoices,
        "balance": float(encaisse) - float(depenses),
    }


@dashboard_bp.route("/")
@login_required
@subscription_required
def index():
    # the admin has their own area
    if current_user.role == "admin":
        return redirect(url_for("admin.index"))

    try:
        stats = _build_stats(current_user.id)
    except Exception:
        # tolerant: if an aggregate fails, display zeros rather than crashing
        stats = {"customers": 0, "quotes": 0, "invoices": 0, "balance": None}

    # NoSQL read: latest activities of the logged-in user (CP6)
    recent_logs = get_recent(limit=10, user_id=current_user.id)
    for log in recent_logs:
        log["label"], log["readable_details"] = describe(log)

    return render_template(
        "dashboard/index.html",
        user=current_user,
        stats=stats,
        recent_logs=recent_logs,
    )
