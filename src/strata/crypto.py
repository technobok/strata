"""Fernet encryption keyed off ``app_setting.secret_key``.

For secrets Strata must read back, which a hash cannot give: connection
parameters (the build attaches with them) and personal API keys (a report page
puts its reader's key into their Excel link). Everything else that only needs
checking, like admin API keys, is stored as a sha256.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


@lru_cache(maxsize=8)
def fernet_for(secret: str) -> Fernet:
    """The Fernet for a secret_key (sha256 of it, urlsafe-base64)."""
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def fernet() -> Fernet:
    """The Fernet of the current database's secret_key (read each call, so a
    process that serves several databases, like the test suite, uses the right one)."""
    from strata.models.app_setting import get_setting  # local: avoid an import cycle

    secret = get_setting("secret_key")
    if not secret:
        raise RuntimeError("secret_key missing from app_setting; run 'strata-admin init-db' first")
    return fernet_for(secret)


def encrypt_text(text: str) -> str:
    return fernet().encrypt(text.encode()).decode()


def decrypt_text(token: str) -> str:
    try:
        return fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("decryption failed (the secret key changed?)") from exc
