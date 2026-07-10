# -*- coding: utf-8 -*-
from __future__ import annotations

"""Local auth helpers for password hashing and opaque session tokens."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple


USER_ROLE_DBA = "dba"
USER_ROLE_RD = "rd"
USER_ROLES = {USER_ROLE_DBA, USER_ROLE_RD}
PASSWORD_ITERATIONS = max(100000, int(os.getenv("DBCHECK_PASSWORD_ITERATIONS", "200000")))
SESSION_TTL_SECONDS = max(1800, int(os.getenv("DBCHECK_SESSION_TTL_SECONDS", "43200")))
BOOTSTRAP_ADMIN_USERNAME = os.getenv("DBCHECK_BOOTSTRAP_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_DISPLAY_NAME = os.getenv("DBCHECK_BOOTSTRAP_ADMIN_DISPLAY_NAME", "DBA Admin")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("DBCHECK_BOOTSTRAP_ADMIN_PASSWORD", "admin")


def hash_password(password: str, salt_hex: Optional[str] = None) -> Tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, password_hash: str) -> bool:
    _salt_hex, digest_hex = hash_password(password, salt_hex=salt_hex)
    return secrets.compare_digest(digest_hex, password_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry_iso(ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    return (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
