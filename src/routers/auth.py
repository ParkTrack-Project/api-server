from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..db_models import GlobalRole, User
from ..dependencies import (
    JWT_EXPIRE_SECONDS,
    CurrentUser,
    create_access_token,
    get_effective_permissions,
    hash_password,
    require,
    verify_password,
)
from ..schemas.auth import (
    AuthUserInfo,
    LoginRequest,
    MeResponse,
    PartnerMembershipInfo,
    RegisterRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ---------------------------------------------------------------------------
# Вспомогательная функция сборки ответа
# ---------------------------------------------------------------------------

def _build_token_response(user: User) -> TokenResponse:
    token = create_access_token(user.user_id)
    return TokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=JWT_EXPIRE_SECONDS,
        user=AuthUserInfo(
            user_id=user.user_id,
            email=user.email,
            full_name=user.full_name,
            global_roles=[user.global_role.value],
        ),
    )


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------

@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=TokenResponse)
def register(body: RegisterRequest, db: Annotated[Session, Depends(get_db)]):
    existing = db.query(User).filter(User.email == body.email).one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_description": "User with this email already exists"},
        )

    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        phone=body.phone,
        global_role=GlobalRole.user,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return _build_token_response(user)


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

@router.post("/login", status_code=status.HTTP_200_OK, response_model=TokenResponse)
def login(body: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(User).filter(User.email == body.login).one_or_none()

    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Invalid login or password"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_description": "Account is disabled"},
        )

    return _build_token_response(user)


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(current_user: CurrentUser):
    # JWT stateless — при наличии sessions-таблицы здесь можно
    # проставить revoked_at. Пока просто возвращаем 204.
    return None


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=MeResponse)
def me(current_user: Annotated[User, require("users.me.view")]):
    effective_permissions = get_effective_permissions(current_user)

    memberships = [
        PartnerMembershipInfo(
            partner_id=m.partner_id,
            role=m.user_role,
            # permissions партнёрского членства не хранятся в БД отдельно —
            # возвращаем пустой список; расширить при необходимости
            permissions=[],
            read_scope=m.read_scope,
            write_scope=m.write_scope,
            delete_scope=m.delete_scope,
        )
        for m in current_user.memberships
    ]

    return MeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        global_roles=[current_user.global_role.value],
        permissions=sorted(effective_permissions),
        partner_memberships=memberships,
    )
