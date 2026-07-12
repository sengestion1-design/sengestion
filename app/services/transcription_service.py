"""Service de transcription audio (Whisper local).

Transcrit un fichier audio (envoyé par le navigateur via MediaRecorder) en texte,
avec le modèle Whisper d'OpenAI exécuté LOCALEMENT (pas d'API cloud, pas de coût).
Compatible tous navigateurs (Safari, Chrome, mobile) contrairement à la Web
Speech API. ffmpeg est requis (déjà présent) pour décoder l'audio du navigateur.

Le modèle est chargé une seule fois puis mis en cache (coûteux à charger).
"""
from __future__ import annotations

import os
import tempfile

from flask import current_app

# Cache module-level du modèle Whisper (chargement long : une seule fois).
_MODEL = None
_MODEL_NAME = "small"   # bon compromis qualité/vitesse pour le français


def _get_model():
    global _MODEL
    if _MODEL is None:
        import whisper
        _MODEL = whisper.load_model(_MODEL_NAME)
    return _MODEL


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> dict:
    """Transcrit un audio en texte français.

    Retourne :
      {"ok": True,  "text": "..."}
      {"ok": False, "error": "message utilisateur"}
    """
    if not audio_bytes:
        return {"ok": False, "error": "Aucun audio reçu."}

    try:
        import whisper  # noqa: F401  (vérifie la dépendance)
    except ImportError:
        return {"ok": False,
                "error": "Module de transcription indisponible sur le serveur."}

    # Écrire l'audio dans un fichier temporaire (Whisper/ffmpeg lit un chemin).
    ext = os.path.splitext(filename)[1] or ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(audio_bytes)
            tmp_path = tf.name

        model = _get_model()
        result = model.transcribe(tmp_path, language="fr", fp16=False)
        text = (result.get("text") or "").strip()
        if not text:
            return {"ok": False,
                    "error": "Aucune parole détectée. Parlez plus près du micro et réessayez."}
        return {"ok": True, "text": text}
    except Exception as exc:  # noqa: BLE001 - garde-fou
        current_app.logger.warning("Transcription error: %s", exc, exc_info=True)
        return {"ok": False,
                "error": "Impossible de transcrire l'audio. Réessayez ou saisissez manuellement."}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
