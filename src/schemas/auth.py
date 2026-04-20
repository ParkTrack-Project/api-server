from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email:     EmailStr
    password:  str   = Field(min_length=6)
    full_name: str | None = Field(None, max_length=255)
    phone:     str | None = Field(None, max_length=50)


class LoginRequest(BaseModel):
    login:    str
    password: str


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------

class AuthUserInfo(BaseModel):
    user_id:      int
    email:        str
    full_name:    str | None
    global_roles: list[str]


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "Bearer"
    expires_in:   int
    user:         AuthUserInfo


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

class PartnerMembershipInfo(BaseModel):
    partner_id:   int
    role:         str
    permissions:  list[str]
    read_scope:   str
    write_scope:  str
    delete_scope: str


class MeResponse(BaseModel):
    user_id:             int
    email:               str
    full_name:           str | None
    global_roles:        list[str]
    permissions:         list[str]
    partner_memberships: list[PartnerMembershipInfo]
