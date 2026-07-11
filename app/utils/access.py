"""Access-control decorators — security (OWASP: access control).

- @subscription_required : blocks managers whose trial/subscription has expired.
- @admin_required        : restricts a route to administrators.
"""
from functools import wraps

from flask import redirect, url_for, abort
from flask_login import current_user

from app.extensions import db


def subscription_required(view):
    """Allow access only if the account is active (valid trial or subscription)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        # refresh status (trial/active -> expired if past due)
        current_user.refresh_status()
        db.session.commit()

        if not current_user.access_allowed:
            return redirect(url_for("subscription.expired"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Restrict the route to administrators."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped
