"""AI service - reading an expense receipt / invoice (Claude Vision).

An expense invoice (JPG/PNG/HEIC photo or PDF) is sent to Claude Vision,
which extracts the accounting information: label (supplier/purpose), total
amount incl. tax and date. The result pre-fills the expense form, which the
manager verifies before saving.

PDFs are first converted to an image (1st page) via pdftoppm (poppler).
All errors return (ok=False, error=...) instead of raising.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile

from flask import current_app

ALLOWED_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp", "gif": "image/gif",
}

_PROMPT = (
    "Tu lis une facture ou un reçu de dépense (achat professionnel). "
    "Extrais les informations comptables et réponds UNIQUEMENT avec un objet JSON "
    "valide, sans texte autour, avec exactement ces clés (valeur \"\" ou 0 si absente) :\n"
    "{\n"
    '  "label": "objet de la dépense (fournisseur ou nature de l\'achat)",\n'
    '  "amount": 0,\n'
    '  "date": "AAAA-MM-JJ",\n'
    '  "category": "catégorie probable parmi : Carburant, Fournitures, Loyer, '
    'Salaires, Transport, Autre"\n'
    "}\n"
    "Le montant est le TOTAL TTC payé, en nombre entier (sans espace ni devise). "
    "La date au format AAAA-MM-JJ. N'invente rien : laisse vide si illisible."
)


def _to_jpeg_bytes(raw: bytes, filename: str, content_type: str | None):
    """Normalize the input to JPEG. Handles images (HEIC included) AND PDFs (1st page).

    Returns (jpeg_bytes, "image/jpeg") or (None, None) if unusable.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    is_pdf = ext == "pdf" or (content_type or "").lower() == "application/pdf"

    if is_pdf:
        # Convert the PDF's 1st page to PNG via pdftoppm, then to JPEG.
        tmp_pdf = tmp_prefix = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                tf.write(raw)
                tmp_pdf = tf.name
            tmp_prefix = tempfile.mktemp()
            subprocess.run(
                ["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", "1",
                 tmp_pdf, tmp_prefix],
                check=True, capture_output=True, timeout=30,
            )
            png_path = tmp_prefix + "-1.png"
            if not os.path.exists(png_path):
                # some poppler builds name the file without the -1
                png_path = tmp_prefix + ".png"
            if not os.path.exists(png_path):
                return None, None
            from PIL import Image
            import io
            img = Image.open(png_path).convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=88)
            return out.getvalue(), "image/jpeg"
        except Exception:
            current_app.logger.warning("Receipt PDF conversion failed", exc_info=True)
            return None, None
        finally:
            for p in (tmp_pdf, (tmp_prefix + "-1.png") if tmp_prefix else None,
                      (tmp_prefix + ".png") if tmp_prefix else None):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # Image: normalize to JPEG via Pillow (handles iPhone HEIC if pillow-heif).
    try:
        from PIL import Image
        import io
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return None, None


def scan_receipt(raw_bytes: bytes, filename: str = "",
                 content_type: str | None = None) -> dict:
    """Read a receipt/invoice and extract label, amount, date, category.

    Returns:
      {"ok": True,  "fields": {label, amount, date, category}, "image": jpeg_bytes}
      {"ok": False, "error": "user-facing message"}
    """
    if not raw_bytes:
        return {"ok": False, "error": "Aucun fichier reçu."}

    jpeg, media_type = _to_jpeg_bytes(raw_bytes, filename, content_type)
    if jpeg is None:
        return {"ok": False,
                "error": "Fichier non lisible. Utilisez une photo (JPG, PNG, HEIC) ou un PDF."}

    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False,
                "error": "La lecture IA n'est pas configurée (clé API manquante)."}
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "Module IA indisponible sur le serveur."}

    b64 = base64.standard_b64encode(jpeg).decode("ascii")
    model = current_app.config.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-5")
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model, max_tokens=512,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type, "data": b64}},
                {"type": "text", "text": _PROMPT},
            ]}],
        )
    except anthropic.APIError as exc:
        current_app.logger.warning("Claude receipt error: %s", exc)
        return {"ok": False,
                "error": "Le service de lecture IA est momentanément indisponible."}
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("Claude receipt unexpected: %s", exc)
        return {"ok": False, "error": "Impossible d'analyser le reçu."}

    raw = "".join(b.text for b in message.content
                  if getattr(b, "type", "") == "text").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return {"ok": False,
                "error": "Reçu non lisible. Réessayez avec une photo plus nette."}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "Lecture IA illisible. Réessayez."}

    # Normalize
    label = str(data.get("label", "")).strip()[:255]
    try:
        amount = int(float(str(data.get("amount", 0)).replace(" ", "").replace(",", ".") or 0))
    except (TypeError, ValueError):
        amount = 0
    d = str(data.get("date", "")).strip()
    category = str(data.get("category", "")).strip()

    if not label and amount <= 0:
        return {"ok": False,
                "error": "Aucune information exploitable détectée sur le reçu."}

    return {"ok": True,
            "fields": {"label": label, "amount": amount, "date": d, "category": category},
            "image": jpeg}
