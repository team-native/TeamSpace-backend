from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refreshToken: str


class UserSummary(BaseModel):
    id: int
    name: str
    roles: list[str]

    model_config = ConfigDict(from_attributes=True)


class LoginData(BaseModel):
    accessToken: str
    refreshToken: str
    user: UserSummary


class CurrentUserData(BaseModel):
    id: int
    name: str
    email: EmailStr
    department: str | None
    roles: list[str]
