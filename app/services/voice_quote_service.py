"""AI service - quote creation by voice command.

The text dictated by the manager (transcribed browser-side via the Web Speech API)
is sent to Claude, which extracts a structured quote from it: customer name + line
items (description, quantity, unit price). The result pre-fills the quote form,
which the manager verifies before saving.

"Wow" AI feature (RNCP), isolated in a service to keep the routes thin.
All errors (missing key, API, unusable text) return a
(ok=False, error=...) result instead of raising an exception.
"""
from __future__ import annotations

import json

from flask import current_app


def _prompt(transcript: str, customer_names: list[str]) -> str:
    """Build the instruction sent to Claude."""
    names = ", ".join(customer_names) if customer_names else "(aucun client enregistré)"
    return (
        "Tu es un assistant qui transforme une commande vocale en devis structuré, "
        "pour une PME sénégalaise (montants en francs CFA, sans décimales).\n\n"
        f"Clients déjà enregistrés : {names}.\n\n"
        "À partir du texte dicté ci-dessous, extrais :\n"
        "- le nom du client (s'il correspond à un client enregistré, reprends EXACTEMENT "
        "son nom ; sinon garde le nom prononcé) ;\n"
        "- les lignes du devis : pour chaque article/prestation, sa désignation, "
        "la quantité (défaut 1 si non précisée) et le prix unitaire en FCFA.\n\n"
        "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour :\n"
        "{\n"
        '  "customer_name": "nom du client",\n'
        '  "items": [\n'
        '    {"description": "désignation", "quantity": 1, "unit_price": 50000}\n'
        "  ]\n"
        "}\n"
        "Règles : quantity et unit_price sont des nombres entiers positifs. "
        "N'invente pas de prix : si un prix n'est pas dit, mets 0. "
        "Ignore les mots de liaison (« et », « avec », « aussi »…).\n\n"
        f"Texte dicté :\n\"\"\"{transcript}\"\"\""
    )


def parse_voice_quote(transcript: str, customer_names: list[str] | None = None) -> dict:
    """Turn dictated text into a structured quote via Claude.

    Returns:
      {"ok": True,  "customer_name": str, "items": [{description, quantity, unit_price}]}
      {"ok": False, "error": "user-facing message"}
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return {"ok": False, "error": "Aucune commande vocale reçue."}

    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False,
                "error": "La reconnaissance IA n'est pas configurée (clé API manquante)."}

    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return {"ok": False, "error": "Module IA indisponible sur le serveur."}

    model = current_app.config.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-5")
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user",
                       "content": _prompt(transcript, customer_names or [])}],
        )
    except anthropic.APIError as exc:  # network / quota / API
        current_app.logger.warning("Claude voice error: %s", exc)
        return {"ok": False,
                "error": "Le service IA est momentanément indisponible. Réessayez."}
    except Exception as exc:  # noqa: BLE001 - safety net
        current_app.logger.warning("Claude voice unexpected error: %s", exc)
        return {"ok": False, "error": "Impossible d'interpréter la commande vocale."}

    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()

    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return {"ok": False,
                "error": "Commande non comprise. Reformulez en précisant client, articles et prix."}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "Réponse IA illisible. Réessayez."}

    # Defensive normalization of the line items.
    items = []
    for it in (data.get("items") or []):
        desc = str(it.get("description", "")).strip()
        if not desc:
            continue
        try:
            qty = int(float(it.get("quantity", 1) or 1))
        except (TypeError, ValueError):
            qty = 1
        try:
            price = int(float(it.get("unit_price", 0) or 0))
        except (TypeError, ValueError):
            price = 0
        items.append({
            "description": desc[:255],
            "quantity": max(1, qty),
            "unit_price": max(0, price),
        })

    if not items:
        return {"ok": False,
                "error": "Aucun article détecté. Dites par exemple : "
                         "« Devis pour Awa, 3 tables à 50000 francs »."}

    return {
        "ok": True,
        "customer_name": str(data.get("customer_name", "")).strip(),
        "items": items,
    }
