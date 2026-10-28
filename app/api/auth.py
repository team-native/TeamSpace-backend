import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import CurrentUserData, LoginRequest, RefreshRequest
from app.services.security import create_token_pair, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/login", summary="사용자 로그인 및 토큰 발급")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 일치하지 않습니다.",
        )

    access_token, refresh_token = create_token_pair(user.id)
    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "user": {"id": str(user.id), "name": user.name, "roles": user.roles},
    }


@router.post("/auth/refresh", summary="JWT 토큰 갱신")
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> dict:
    try:
        claims = jwt.decode(
            payload.refreshToken,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if claims.get("type") != "refresh":
            raise HTTPException(
                status_code=401, detail="유효하지 않은 refresh token입니다."
            )
        user_id = int(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=401, detail="refresh token이 유효하지 않거나 만료되었습니다."
        ) from None
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")
    access_token, refresh_token = create_token_pair(user.id)
    return {"accessToken": access_token, "refreshToken": refresh_token}


@router.get("/users/me", summary="프로필 및 권한 조회", response_model=None)
def get_profile(user: User = Depends(get_current_user)) -> dict:
    data = CurrentUserData(
        id=user.id,
        name=user.name,
        email=user.email,
        department=user.department,
        roles=user.roles,
    )
    return data.model_dump()
