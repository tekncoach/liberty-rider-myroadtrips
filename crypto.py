"""Encryption at rest for the Liberty Rider credentials stored in `users`.

Why: `bearer_token`, `refresh_token` and `firebase_api_key` are enough to act
as the user on Liberty Rider, and the refresh token never expires on its own
— a database dump (or a stray backup) is a durable account takeover, for
third parties as much as for the operator. Finding APP-01 of the audit.

Key handling: `TOKEN_ENCRYPTION_KEY` holds a Fernet key (generate one with
`python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`), injected out of band — on the live
VM through the systemd `EnvironmentFile=/etc/roadtrips.env`, which is already
root-owned and 0600. It must never live in the database it protects, nor in
the repository.

No key set means no encryption: values are read and written in clear, which
is what local development and the test suite do. That is a deliberate
fallback, not an oversight — making the key mandatory would turn a missing
env var into a login outage. The startup log says which mode is active.

Stored values carry an `enc:v1:` marker, so plaintext written before the key
existed keeps being readable and gets migrated in place
(`db.encrypt_plaintext_tokens`). The marker is also what makes a future key
rotation implementable: read with the old key, write with the new one.
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("carnet.crypto")

PREFIX = "enc:v1:"

_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
try:
    _FERNET = Fernet(_KEY.encode()) if _KEY else None
except (ValueError, TypeError):
    # A malformed key must not silently degrade to storing tokens in clear:
    # that would be the one failure mode this module exists to prevent.
    raise RuntimeError(
        "TOKEN_ENCRYPTION_KEY is set but is not a valid Fernet key — "
        'generate one with: python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    ) from None


def is_enabled() -> bool:
    return _FERNET is not None


def encrypt(value: str | None) -> str | None:
    """Encrypt for storage. Idempotent: an already-encrypted value passes
    through, so callers never have to track what state a value is in."""
    if value is None or _FERNET is None or value.startswith(PREFIX):
        return value
    return PREFIX + _FERNET.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    """Decrypt on read. Plaintext (no marker) is returned as-is — that's how
    rows written before the key existed stay usable.

    An unreadable value returns None rather than raising: the key was rotated
    or lost, and the caller's own "no token on file" path already asks the
    user to log in again. A 401 is the right answer here; a 500 is not.
    """
    if value is None or not value.startswith(PREFIX):
        return value
    if _FERNET is None:
        logger.error("encrypted token found but TOKEN_ENCRYPTION_KEY is not set")
        return None
    try:
        return _FERNET.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("stored token could not be decrypted — wrong or rotated key")
        return None
