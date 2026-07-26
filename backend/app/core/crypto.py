"""Symmetric encryption for secrets held at rest (plan §30).

OAuth refresh tokens are long-lived mailbox credentials, so a database dump
must not be enough to read an agency's mail. `TOKEN_ENCRYPTION_KEY` lives only
in the environment, never in the database, so the two have to be stolen
separately.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import settings


def _is_real_fernet_key(value: str) -> bool:
    """True only for a genuine Fernet key: 32 raw bytes, url-safe base64.

    Pure so both branches are testable without touching the environment.
    """
    try:
        return len(base64.urlsafe_b64decode(value.encode())) == 32
    except (ValueError, TypeError):
        return False


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.TOKEN_ENCRYPTION_KEY
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set — refusing to store secrets in clear.")
    if settings.is_production:
        # Deriving a key by hashing would silently accept a placeholder such as
        # "changeme" and encrypt every mailbox credential under a guessable key.
        # In production the operator must supply real entropy:
        #   python -c "from cryptography.fernet import Fernet;
        #              print(Fernet.generate_key().decode())"
        if not _is_real_fernet_key(key):
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY must be a 32-byte url-safe base64 Fernet key in production "
                "(generate one with cryptography.fernet.Fernet.generate_key). See docs/setup.md."
            )
        return Fernet(key)
    # Outside production, accept any operator-supplied string: CI and local
    # development supply plain placeholders and encrypt nothing that matters.
    digest = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
