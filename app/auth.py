from datetime import datetime, timedelta, timezone
import secrets

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.config import get_settings


settings = get_settings()
bearer_auth = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="JWT bearer token for authenticated operations.")


def generate_api_key() -> str:
    return f"bk_{secrets.token_urlsafe(24)}"


def create_user_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.user_jwt_ttl_minutes)
    payload = {"sub": user_id, "exp": exp}
    return jwt.encode(payload, settings.user_jwt_secret, algorithm="HS256")


def decode_user_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.user_jwt_secret, algorithms=["HS256"])
        sub = payload.get("sub")
        if not isinstance(sub, str):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "UNAUTHORIZED", "message": "Invalid token payload"})
        return sub
    except InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "UNAUTHORIZED", "message": "Invalid or expired token"}) from exc


def get_current_user_id(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth)) -> str:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "UNAUTHORIZED", "message": "Missing Bearer token"})
    token = credentials.credentials
    return decode_user_token(token)


def require_user(user_id: str, current_user_id: str = Depends(get_current_user_id)) -> str:
    if user_id != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "FORBIDDEN", "message": "You can only access your own resources"})
    return current_user_id
