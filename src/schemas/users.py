from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserResponse(BaseModel):
    user_id:    int
    email:      str
    full_name:  str | None
    phone:      str | None
    global_role: str
    is_active:  bool
    created_at: datetime
    updated_at: datetime


class UpdateUserRequest(BaseModel):
    full_name: str | None = Field(None, max_length=255)
    phone:     str | None = Field(None, max_length=50)
    email:     EmailStr   | None = None


class UpdatePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class AdminUpdateUserRequest(BaseModel):
    """Только для администратора — позволяет менять роль и статус."""
    full_name:   str | None = Field(None, max_length=255)
    phone:       str | None = Field(None, max_length=50)
    email:       EmailStr   | None = None
    global_role: str        | None = None
    is_active:   bool       | None = None


class UserListResponse(BaseModel):
    items:  list[UserResponse]
    total:  int
    top:    int
    offset: int
