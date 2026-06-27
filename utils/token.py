"""Unique token generation for share links."""
import secrets
import string

ALPHABET = string.ascii_letters + string.digits


def generate_token(length: int = 12) -> str:
    """Generate a URL-safe random token."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
