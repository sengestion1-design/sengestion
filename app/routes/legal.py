"""Public legal pages — GDPR compliance (REAC DWWM: setting up the legal
notices required by the General Data Protection Regulation).

Three pages accessible without login:
- /mentions-legales   : publisher, host, intellectual property
- /confidentialite    : privacy policy (data, purposes, rights)
- /cookies            : cookie policy (a single technical session cookie)
"""
from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__)


@legal_bp.route("/mentions-legales")
def mentions():
    return render_template("legal/mentions.html")


@legal_bp.route("/confidentialite")
def confidentialite():
    return render_template("legal/confidentialite.html")


@legal_bp.route("/cookies")
def cookies():
    return render_template("legal/cookies.html")
