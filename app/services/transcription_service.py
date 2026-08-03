"""Audio transcription service (local Whisper).

Transcribes an audio file (sent by the browser via MediaRecorder) into text,
using OpenAI's Whisper model run LOCALLY (no cloud API, no cost).
Compatible with all browsers (Safari, Chrome, mobile) unlike the Web
Speech API. ffmpeg is required (already present) to decode the browser audio.

The model is loaded once then cached (expensive to load).
"""
from __future__ import annotations

import os
import tempfile

from flask import current_app

# Module-level cache of the Whisper model (long load: only once).
# 'base': ~10x faster than 'small' on CPU (~2-3s), sufficient quality since
# Claude then corrects names using the list of existing customers.
_MODEL = None
_MODEL_NAME = "base"


def _get_model():
    global _MODEL
    if _MODEL is None:
        import whisper
        _MODEL = whisper.load_model(_MODEL_NAME)
    return _MODEL


def preload_model_async():
    """Preload the Whisper model in the background at app startup,
    so the first transcription is fast (no loading latency)."""
    import threading

    def _load():
        try:
            _get_model()
        except Exception:
            pass  # best-effort: if it fails, loading will happen on the 1st request

    threading.Thread(target=_load, daemon=True).start()


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> dict:
    """Transcribe audio into French text.

    Returns:
      {"ok": True,  "text": "..."}
      {"ok": False, "error": "user-facing message"}
    """
    if not audio_bytes:
        return {"ok": False, "error": "Aucun audio reçu."}

    try:
        import whisper  # noqa: F401  (checks the dependency)
    except ImportError:
        return {"ok": False,
                "error": "Module de transcription indisponible sur le serveur."}

    # Write the audio to a temporary file (Whisper/ffmpeg reads a path).
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
    except Exception as exc:  # noqa: BLE001 - safety net
        current_app.logger.warning("Transcription error: %s", exc, exc_info=True)
        return {"ok": False,
                "error": "Impossible de transcrire l'audio. Réessayez ou saisissez manuellement."}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
