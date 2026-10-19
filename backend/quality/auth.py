import hmac
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException

from quality.domain import Actor, Role

# 仅在本地演示模式启用；生产环境禁止此登录入口。
DEMO_USERS = {
    "merchant-a": Actor(user_id="merchant-a", role=Role.MERCHANT, merchant_id="merchant-a"),
    "merchant-b": Actor(user_id="merchant-b", role=Role.MERCHANT, merchant_id="merchant-b"),
    "reviewer": Actor(user_id="reviewer", role=Role.REVIEWER),
    "admin": Actor(user_id="admin", role=Role.ADMIN),
}


def login(username: str, password: str, settings) -> tuple[str, Actor]:
    if not settings.demo_login_enabled:
        raise HTTPException(404, "演示登录未启用")
    actor = DEMO_USERS.get(username)
    if actor is None or not hmac.compare_digest(password.encode("utf-8"), b"demo-only"):
        raise HTTPException(401, "用户名或密码错误")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": actor.user_id, "iat": now, "exp": now + timedelta(hours=4)},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return token, actor


def authenticate(token: str, settings) -> Actor:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
        return DEMO_USERS[payload["sub"]]
    except (jwt.InvalidTokenError, KeyError, TypeError):
        raise HTTPException(401, "登录状态无效或已过期") from None
