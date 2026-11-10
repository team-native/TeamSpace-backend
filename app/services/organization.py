import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.resource import Resource
from app.models.user import User

ORGANIZATION_PATH = Path(__file__).resolve().parents[1] / "data" / "organization.json"


def load_organization() -> dict[str, Any]:
    with ORGANIZATION_PATH.open(encoding="utf-8") as organization_file:
        return json.load(organization_file)


def find_directory_member(member_id: str) -> dict[str, Any] | None:
    return next(
        (
            member
            for member in load_organization().get("members", [])
            if member.get("id") == member_id
        ),
        None,
    )


def find_user_for_member(db: Session, member: dict[str, Any]) -> User | None:
    mapped_users = find_explicit_users_for_member(db, member)
    if mapped_users:
        return mapped_users[0]
    return None


def find_explicit_users_for_member(db: Session, member: dict[str, Any]) -> list[User]:
    profiles = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "profile",
                Resource.payload["organizationMemberId"].as_string()
                == member.get("id"),
            )
        )
    )
    mapped_users = [
        user
        for profile in profiles
        if (user := db.get(User, profile.owner_id)) is not None
    ]
    if len(mapped_users) > 1:
        raise HTTPException(409, "조직도 멤버에 여러 계정이 연결되어 있습니다.")
    return mapped_users


def _profile_member_id(db: Session, user_id: int) -> str | None:
    profile = db.scalar(
        select(Resource).where(
            Resource.resource_type == "profile", Resource.owner_id == user_id
        )
    )
    return profile.payload.get("organizationMemberId") if profile else None


def get_user_by_public_id(db: Session, public_id: str) -> User | None:
    if public_id.isdecimal():
        return db.get(User, int(public_id))
    member = find_directory_member(public_id)
    if member is None:
        return None
    return find_user_for_member(db, member)


def get_public_user_id(db: Session, user: User) -> str:
    member_id = _profile_member_id(db, user.id)
    if member_id and find_directory_member(member_id):
        return str(member_id)
    return str(user.id)
