from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import MAX_PASSWORD_BYTES
from app.core.time import UTCDateTime
from app.models.user import Role
from app.schemas.common import ORMModel

# A short list of the most common passwords that pass the letter-and-digit rule.
COMMON_PASSWORDS = {"password1", "password123", "passw0rd", "qwerty123", "abc12345", "abcd1234", "1q2w3e4r",
                    "iloveyou1", "welcome1", "admin123", "letmein1", "12345678a", "a12345678"}


class PasswordRules(BaseModel):
    """The password rules, shared by registration, admin-created accounts and password resets."""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=8, max_length=64)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, value: str) -> str:
        if len(value.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
        return value

    @field_validator("password")
    @classmethod
    def password_not_trivial(cls, value: str) -> str:
        if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
            raise ValueError("password must contain at least one letter and one digit")
        if len(set(value.lower())) < 4 or value.lower() in COMMON_PASSWORDS:
            raise ValueError("password is too easy to guess")
        return value


class RegisterRequest(PasswordRules):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr


class UserOut(ORMModel):
    id: int
    username: str
    email: str
    role: Role
    is_active: bool
    created_at: UTCDateTime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
