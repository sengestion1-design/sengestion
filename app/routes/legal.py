"""Pages légales publiques — conformité RGPD (REAC DWWM : mise en place
des mentions légales liées au Règlement Général sur la Protection des Données).

Trois pages accessibles sans connexion :
- /mentions-legales   : éditeur, hébergeur, propriété intellectuelle
- /confidentialite    : politique de confidentialité (données, finalités, droits)
- /cookies            : politique cookies (un seul cookie technique de session)
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
