"""Credential handling.

- API keys: 32 random bytes, shown once, stored as SHA-256 (keys are high-entropy,
  so a fast hash is appropriate; no salt/slow KDF needed). Lookup is by hash.
- Passwords: scrypt (stdlib) with per-user salt.
- Upstream credentials: Fernet (AES-128-CBC + HMAC) with a key supplied via Secret.
- Sessions: HMAC-signed, expiring token in an HttpOnly cookie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

KEY_PREFIX = "umk_"


def new_api_key() -> tuple[str, str, str]:
    """-> (plaintext, display_prefix, sha256_hex)"""
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    return plain, plain[:10], hash_api_key(plain)


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def new_invite_token() -> tuple[str, str]:
    t = "inv_" + secrets.token_urlsafe(24)
    return t, hashlib.sha256(t.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, s, d = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(d))
    except Exception:
        return False


class Vault:
    """Encrypts/decrypts upstream credentials."""

    def __init__(self, key: str):
        if not key:
            raise RuntimeError("GATEWAY_ENCRYPTION_KEY is required")
        self._f = Fernet(key.encode())

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise RuntimeError("cannot decrypt credential (wrong GATEWAY_ENCRYPTION_KEY?)") from e


class SessionSigner:
    def __init__(self, secret: str, ttl: int):
        if not secret:
            raise RuntimeError("GATEWAY_SESSION_SECRET is required")
        self._k, self._ttl = secret.encode(), ttl

    def _sig(self, body: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self._k, body, hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self, user_id: int) -> str:
        body = base64.urlsafe_b64encode(json.dumps({"u": user_id, "exp": int(time.time()) + self._ttl}).encode())
        return f"{body.decode()}.{self._sig(body)}"

    def verify(self, token: str | None) -> int | None:
        try:
            body, sig = (token or "").split(".")
            if not hmac.compare_digest(sig, self._sig(body.encode())):
                return None
            data = json.loads(base64.urlsafe_b64decode(body))
            return data["u"] if data["exp"] > time.time() else None
        except Exception:
            return None
