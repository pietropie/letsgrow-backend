import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: str | None = None

    @field_validator("username")
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Username deve conter apenas letras, numeros, _ ou -")
        if len(v) < 3 or len(v) > 50:
            raise ValueError("Username deve ter entre 3 e 50 caracteres")
        return v.lower()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Senha deve ter no minimo 8 caracteres")
        return v


class RegisterResponse(BaseModel):
    """Retornado pelo /register -- sem tokens (usuario precisa verificar o email primeiro)."""
    email: str


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class OAuthGoogleRequest(BaseModel):
    id_token: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    username: str
    full_name: str | None
    avatar_url: str | None
    plan: str
    plan_expires_at: datetime | None
    is_active: bool
    is_verified: bool
    is_dev_mode: bool
    created_at: datetime

    model_config = {"from_attributes": True}
