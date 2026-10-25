import base64
import hashlib
import uuid

import jwt
import pytest
from cryptography.fernet import Fernet

from app.core.encryption import DecryptionError, TokenCipher
from app.core.exceptions import AuthenticationError
from app.core.logging import REDACTED, redact
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_pkce_pair,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip_uses_argon2():
    hashed = hash_password("a-very-long-password")
    assert hashed.startswith("$argon2id$")
    assert verify_password("a-very-long-password", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("anything", None)


def test_access_token_roundtrip_and_tamper():
    user_id = uuid.uuid4()
    token, ttl = create_access_token(user_id)
    assert ttl == 15 * 60
    assert decode_access_token(token)["sub"] == str(user_id)
    with pytest.raises(AuthenticationError):
        decode_access_token(token[:-2] + "xx")


def test_access_token_rejects_other_types_and_algorithms():
    forged = jwt.encode({"sub": "x", "type": "refresh", "iat": 0, "exp": 9999999999},
                        "test-secret-key-that-is-long-enough-for-hs256-usage", algorithm="HS256")
    with pytest.raises(AuthenticationError):
        decode_access_token(forged)
    unsigned = jwt.encode({"sub": "x", "type": "access", "iat": 0, "exp": 9999999999}, None, algorithm="none")
    with pytest.raises(AuthenticationError):
        decode_access_token(unsigned)


def test_pkce_pair_is_s256():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_token_cipher_roundtrip_rotation_and_tamper():
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old = TokenCipher([old_key])
    ciphertext = old.encrypt("ya29.secret-token")
    assert "secret-token" not in ciphertext
    rotated_cipher = TokenCipher([new_key, old_key])
    assert rotated_cipher.decrypt(ciphertext) == "ya29.secret-token"
    rotated = rotated_cipher.rotate(ciphertext)
    assert TokenCipher([new_key]).decrypt(rotated) == "ya29.secret-token"
    with pytest.raises(DecryptionError):
        TokenCipher([new_key]).decrypt(ciphertext)
    assert old.encrypt(None) is None


def test_log_redaction():
    data = redact({"access_token": "abc", "nested": {"password": "p", "ok": 1}, "header": "Bearer abc.def"})
    assert data["access_token"] == REDACTED
    assert data["nested"]["password"] == REDACTED
    assert data["nested"]["ok"] == 1
    assert "abc.def" not in data["header"]
