from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

password_hash = PasswordHash.recommended()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def hash_password(plain_password: str) -> str:
    return password_hash.hash(plain_password)


def create_token(user_id: int, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )


def create_token_pair(user_id: int) -> tuple[str, str]:
    access_token = create_token(
        user_id,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
    )
    refresh_token = create_token(
        user_id,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
    )
    return access_token, refresh_token
