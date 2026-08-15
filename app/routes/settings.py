"""Settings module - company identity, logo/signature/stamp, email settings.

Security:
- @login_required + @subscription_required.
- Each manager only accesses THEIR settings (1-1 relationship via user_id).
- Normalized image uploads (Pillow) + generated names (uuid) → no path traversal.
- Global CSRF (Flask-WTF) on the POST.
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
from app.services.crypto_service import encrypt
from app.utils.access import subscription_required

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

# Managed image fields (columns): logo + signed stamp.
_IMAGE_FIELDS = ("logo", "stamp")


def _clean(value, maxlen):
    return (value or "").strip()[:maxlen]


def _save_setting_image(file, field: str) -> str | None:
    """Normalize and save a settings image. Returns the path relative
    to /static, or None if no file / failure.

    The logo/signature/stamp keep transparency (PNG) - useful for
    overlaying a stamp on a PDF.
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
        # Keep transparency if present (RGBA), else RGB.
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
        current_app.logger.warning("Failed to upload settings image (%s)", field, exc_info=True)
        return None


@settings_bp.route("/")
@login_required
@subscription_required
def index():
    """Display the manager's company settings page."""
    s = CompanySettings.get_or_create(current_user.id)
    return render_template("settings/index.html", s=s)


@settings_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def update():
    """Save the settings (text + image uploads)."""
    s = CompanySettings.get_or_create(current_user.id)

    # --- Text fields ---
    s.company_name = _clean(request.form.get("company_name"), 150)
    s.address = (request.form.get("address") or "").strip()
    s.phone = _clean(request.form.get("phone"), 40)
    s.email = _clean(request.form.get("email"), 150)
    s.website = _clean(request.form.get("website"), 150)
    s.ninea = _clean(request.form.get("ninea"), 50)
    s.rccm = _clean(request.form.get("rccm"), 50)
    s.rc = _clean(request.form.get("rc"), 50)
    s.legal_form = _clean(request.form.get("legal_form"), 50)
    s.capital = _clean(request.form.get("capital"), 50)
    s.footer_note = (request.form.get("footer_note") or "").strip()
    s.email_sender_name = _clean(request.form.get("email_sender_name"), 120)
    s.email_signature = (request.form.get("email_signature") or "").strip()

    # --- Manager's own SMTP account (optional) ---
    if request.form.get("smtp_remove") == "1":
        s.smtp_host = None
        s.smtp_port = None
        s.smtp_use_tls = True
        s.smtp_email = None
        s.smtp_password_encrypted = None
    else:
        smtp_host = _clean(request.form.get("smtp_host"), 150)
        smtp_email = _clean(request.form.get("smtp_email"), 150)
        smtp_password = (request.form.get("smtp_password") or "").strip()
        smtp_port = request.form.get("smtp_port", type=int)

        if smtp_host and smtp_email:
            s.smtp_host = smtp_host
            s.smtp_port = smtp_port or 587
            s.smtp_use_tls = request.form.get("smtp_use_tls") == "1"
            s.smtp_email = smtp_email
            # Only overwrite the stored password if a new one was typed
            # (the field is left blank when re-displaying the form).
            if smtp_password:
                s.smtp_password_encrypted = encrypt(smtp_password)

    # --- Images (uploaded only if a new file is provided) ---
    for field in _IMAGE_FIELDS:
        # Explicit removal requested?
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
            # replace the old image
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
