"""
Utility functions for encrypting and decrypting sensitive data.
"""
from cryptography.fernet import Fernet
from django.conf import settings
import base64
import hashlib


def get_encryption_key() -> bytes:
    """
    Get or generate encryption key from settings.

    Uses SECRET_KEY as base for generating a consistent encryption key.
    """
    secret_key = settings.SECRET_KEY.encode('utf-8')
    # Generate a 32-byte key from SECRET_KEY using SHA256
    key = hashlib.sha256(secret_key).digest()
    # Fernet requires a URL-safe base64-encoded 32-byte key
    return base64.urlsafe_b64encode(key)


def encrypt_token(token: str) -> str:
    """
    Encrypt a token string.

    Args:
        token: The token string to encrypt

    Returns:
        Encrypted token as base64 string
    """
    if not token:
        return ""
    f = Fernet(get_encryption_key())
    encrypted = f.encrypt(token.encode('utf-8'))
    return encrypted.decode('utf-8')


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt an encrypted token string.

    Args:
        encrypted_token: The encrypted token string

    Returns:
        Decrypted token string
    """
    if not encrypted_token:
        return ""
    f = Fernet(get_encryption_key())
    decrypted = f.decrypt(encrypted_token.encode('utf-8'))
    return decrypted.decode('utf-8')

