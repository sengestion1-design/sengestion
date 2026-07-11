"""Module Paramètres — identité entreprise, logo/signature/cachet, réglages email.

Sécurité :
- @login_required + @subscription_required.
- Chaque gérant n'accède qu'à SES paramètres (relation 1-1 via user_id).
- Uploads d'images normalisés (Pillow) + noms générés (uuid) → pas de path traversal.
- CSRF global (Flask-WTF) sur le POST.
"""
import io
import os
import uuid

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.settings import CompanySettings
from app.services.activity_log_service import log_action
from app.utils.access import subscription_required

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

# Champs image gérés : (nom du champ form/colonne, formats)
_IMAGE_FIELDS = ("logo", "signature", "stamp")


def _clean(value, maxlen):
    return (value or "").strip()[:maxlen]


def _save_setting_image(file, field: str) -> str | None:
    """Normalise et enregistre une image de paramètre. Retourne le chemin
    relatif à /static, ou None si pas de fichier / échec.

    Le logo/la signature/le cachet gardent la transparence (PNG) — utile pour
    superposer un cachet sur un PDF.
    """
    if file is None or not file.filename:
        return None
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass

        raw = file.read()
        if not raw:
            return None
        img = Image.open(io.BytesIO(raw))
        img.load()
        # On garde la transparence si présente (RGBA), sinon RGB.
        fmt, ext = ("PNG", "png") if img.mode in ("RGBA", "LA", "P") else ("JPEG", "jpg")
        if fmt == "JPEG" and img.mode != "RGB":
            img = img.convert("RGB")

        rel_dir = os.path.join("uploads", "settings")
        abs_dir = os.path.join(current_app.static_folder, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        fname = f"u{current_user.id}-{field}-{uuid.uuid4().hex[:10]}.{ext}"
        out = io.BytesIO()
        img.save(out, format=fmt)
        with open(os.path.join(abs_dir, fname), "wb") as fh:
            fh.write(out.getvalue())
        return f"{rel_dir}/{fname}"
    except Exception:
        current_app.logger.warning("Échec upload image paramètre (%s)", field, exc_info=True)
        return None


@settings_bp.route("/")
@login_required
@subscription_required
def index():
    """Affiche la page des paramètres de l'entreprise du gérant."""
    s = CompanySettings.get_or_create(current_user.id)
    return render_template("settings/index.html", s=s)


@settings_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def update():
    """Enregistre les paramètres (texte + uploads d'images)."""
    s = CompanySettings.get_or_create(current_user.id)

    # --- Champs texte ---
    s.company_name = _clean(request.form.get("company_name"), 150)
    s.address = (request.form.get("address") or "").strip()
    s.phone = _clean(request.form.get("phone"), 40)
    s.email = _clean(request.form.get("email"), 150)
    s.website = _clean(request.form.get("website"), 150)
    s.ninea = _clean(request.form.get("ninea"), 50)
    s.rccm = _clean(request.form.get("rccm"), 50)
    s.footer_note = (request.form.get("footer_note") or "").strip()
    s.email_sender_name = _clean(request.form.get("email_sender_name"), 120)
    s.email_signature = (request.form.get("email_signature") or "").strip()

    # --- Images (uploadées seulement si un nouveau fichier est fourni) ---
    for field in _IMAGE_FIELDS:
        # Suppression explicite demandée ?
        if request.form.get(f"remove_{field}") == "1":
            old = getattr(s, field)
            if old and old.startswith("uploads/settings/") and ".." not in old:
                try:
                    os.remove(os.path.join(current_app.static_folder, old))
                except OSError:
                    pass
            setattr(s, field, None)
            continue
        path = _save_setting_image(request.files.get(field), field)
        if path:
            # remplacer l'ancienne image
            old = getattr(s, field)
            if old and old.startswith("uploads/settings/") and ".." not in old:
                try:
                    os.remove(os.path.join(current_app.static_folder, old))
                except OSError:
                    pass
            setattr(s, field, path)

    db.session.commit()
    log_action(current_user.id, "update_settings", {"company": s.company_name})
    flash("Paramètres enregistrés.", "success")
    return redirect(url_for("settings.index"))
