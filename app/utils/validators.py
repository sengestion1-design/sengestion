"""Validators - security rules (CNIL 2022 / RGPD art. 32)."""
import re

PASSWORD_MIN_LENGTH = 12


def validate_password(password: str) -> str | None:
    """Check a password against the CNIL 2022 policy (password-only case).

    Requires >= 12 characters with at least one uppercase, one lowercase,
    one digit and one special character.

    Returns None if valid, or a French error message otherwise.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Le mot de passe doit contenir au moins {PASSWORD_MIN_LENGTH} caractères."
    if not re.search(r"[A-Z]", password):
        return "Le mot de passe doit contenir au moins une majuscule."
    if not re.search(r"[a-z]", password):
        return "Le mot de passe doit contenir au moins une minuscule."
    if not re.search(r"\d", password):
        return "Le mot de passe doit contenir au moins un chiffre."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Le mot de passe doit contenir au moins un caractère spécial."
    return None
