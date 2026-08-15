"""Payment proof upload service - normalize + save the subscription payment screenshot."""
import io
import os
import uuid

from flask import current_app


def normalize_to_jpeg(raw: bytes):
    """Normalize any uploaded image (HEIC included) to JPEG bytes, or None if unusable."""
    try:
        from PIL import Image
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
        return out.getvalue()
    except Exception:
        return None


def save_proof(jpeg_bytes: bytes, user_id: int):
    """Save the payment proof under /static/uploads/paiements/, return the relative path."""
    try:
        rel_dir = os.path.join("uploads", "paiements")
        abs_dir = os.path.join(current_app.static_folder, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        # Full 128-bit UUID: the name must be unguessable (financial document);
        # access is also gated by the /static/uploads/paiements/ guard.
        fname = f"u{user_id}-{uuid.uuid4().hex}.jpg"
        with open(os.path.join(abs_dir, fname), "wb") as fh:
            fh.write(jpeg_bytes)
        return f"{rel_dir}/{fname}"
    except Exception:
        current_app.logger.warning("Failed to save payment proof", exc_info=True)
        return None
