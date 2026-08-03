"""IA service - business-card scanning with Claude Vision.

An uploaded business-card image is sent to Anthropic's Claude Vision model, which
returns the contact fields (name, company, role, email, phone) as structured JSON.
The parsed data is used to pre-fill the contact creation form.

This is one of the project's "wow" AI features (RNCP). It stays fully isolated in
a service so the routes remain thin, and every failure mode is handled gracefully:
missing API key, network/API error, or an image Claude can't read all return a
clear (ok=False, error=...) result instead of raising.
"""
from __future__ import annotations

import base64
import json

from flask import current_app

# Accepted image formats → media type sent to the Vision API.
ALLOWED_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}

# Instruction given to Claude: extract ONLY the useful fields, as strict JSON.
_EXTRACTION_PROMPT = (
    "Tu es un assistant qui lit des cartes de visite professionnelles. "
    "Analyse l'image fournie et extrais les informations de contact. "
    "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, avec "
    "exactement ces clés (valeur \"\" si l'information est absente) :\n"
    '{\n'
    '  "name": "nom de famille de la personne",\n'
    '  "first_name": "prénom de la personne",\n'
    '  "company": "nom de l\'entreprise",\n'
    '  "role": "fonction / poste",\n'
    '  "email": "adresse e-mail",\n'
    '  "phone": "numéro de téléphone (format tel quel)"\n'
    '}\n'
    "N'invente aucune donnée : si un champ n'est pas lisible, laisse une chaîne vide."
)


def _detect_media_type(filename: str, fallback: str | None) -> str | None:
    """Return the API media-type from the file extension, else the browser mime."""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[1].lower()
        if ext in ALLOWED_MIME:
            return ALLOWED_MIME[ext]
    if fallback in ALLOWED_MIME.values():
        return fallback
    return None


def _empty_fields() -> dict:
    return {"name": "", "first_name": "", "company": "",
            "role": "", "email": "", "phone": ""}


def scan_business_card(image_bytes: bytes, filename: str = "",
                       content_type: str | None = None) -> dict:
    """Extract contact fields from a business-card image via Claude Vision.

    Returns a dict:
      {"ok": True,  "fields": {name, first_name, company, role, email, phone}}
      {"ok": False, "error": "user-facing message"}
    """
    if not image_bytes:
        return {"ok": False, "error": "Aucune image reçue."}

    media_type = _detect_media_type(filename, content_type)
    if media_type is None:
        return {"ok": False,
                "error": "Format non supporté. Utilisez JPG, PNG ou WebP."}

    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        # No API key configured: report it clearly (demo / missing config).
        return {"ok": False,
                "error": "La lecture IA n'est pas configurée (clé API manquante). "
                         "Vous pouvez saisir le contact manuellement."}

    try:
        import anthropic
    except ImportError:  # pragma: no cover - dependency declared in requirements
        return {"ok": False,
                "error": "Module IA indisponible sur le serveur."}

    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    model = current_app.config.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-5")

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    }},
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                ],
            }],
        )
    except anthropic.APIError as exc:  # network / API / quota errors
        current_app.logger.warning("Claude Vision error: %s", exc)
        return {"ok": False,
                "error": "Le service de lecture IA est momentanément indisponible. "
                         "Réessayez ou saisissez le contact manuellement."}
    except Exception as exc:  # safety net: never crash the request
        current_app.logger.warning("Claude Vision unexpected error: %s", exc)
        return {"ok": False,
                "error": "Impossible d'analyser l'image. "
                         "Saisissez le contact manuellement."}

    # Concatenate the text blocks of the response
    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()

    # Claude may wrap the JSON in ```; isolate the {...} object.
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return {"ok": False,
                "error": "La carte n'a pas pu être lue. Réessayez avec une photo plus nette."}

    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"ok": False,
                "error": "Lecture IA illisible. Réessayez avec une photo plus nette."}

    # Normalize: keep only our keys, force strings.
    fields = _empty_fields()
    for key in fields:
        value = data.get(key, "")
        fields[key] = str(value).strip() if value is not None else ""

    # At least one meaningful field must be present.
    if not any([fields["name"], fields["company"], fields["email"], fields["phone"]]):
        return {"ok": False,
                "error": "Aucune information exploitable détectée sur la carte."}

    return {"ok": True, "fields": fields}
