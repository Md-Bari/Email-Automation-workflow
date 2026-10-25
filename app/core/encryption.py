"""Encryption at rest for OAuth credentials (Fernet: AES-128-CBC + HMAC-SHA256).

``TOKEN_ENCRYPTION_KEYS`` accepts several comma-separated keys: the first key encrypts new
values and every key can decrypt, which allows key rotation without downtime.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class DecryptionError(Exception):
    pass


class TokenCipher:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("At least one encryption key is required")
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str | None) -> str | None:
        if ciphertext is None:
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise DecryptionError("Unable to decrypt stored credential") from exc

    def rotate(self, ciphertext: str) -> str:
        return self._fernet.rotate(ciphertext.encode()).decode()


@lru_cache
def get_cipher() -> TokenCipher:
    settings = get_settings()
    keys = [k.get_secret_value() for k in settings.token_encryption_keys]
    if not keys:
        if settings.environment == "production":
            raise RuntimeError("TOKEN_ENCRYPTION_KEYS must be configured in production")
        # Development convenience only: an ephemeral key means stored tokens do not survive restarts.
        logger.warning("token_encryption_key_missing_using_ephemeral_key")
        keys = [Fernet.generate_key().decode()]
    return TokenCipher(keys)
