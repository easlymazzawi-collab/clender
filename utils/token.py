"""Unique token generation for share links."""
import secrets
import string

ALPHABET = string.ascii_letters + string.digits


def generate_token(length: int = 12) -> str:
    """Generate a URL-safe alphanumeric token."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def generate_numeric_token(length: int = 16) -> str:
    """
    Generate a numeric-only token (giống các bot share file phổ biến:
    t.me/bot?start=7754133249527383).
    First digit is 1-9 to avoid leading zeros.
    """
    first = secrets.choice("123456789")
    rest  = "".join(secrets.choice(string.digits) for _ in range(length - 1))
    return first + rest
