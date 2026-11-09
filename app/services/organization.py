import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    matches = list(db.scalars(select(User).where(User.name == member.get("name", ""))))
    if len(matches) > 1:
        raise HTTPException(
            409, "동명이인이 있어 조직도 ID를 계정에 연결할 수 없습니다."
        )
    return matches[0] if matches else None


def get_user_by_public_id(db: Session, public_id: str) -> User | None:
    if public_id.isdecimal():
        return db.get(User, int(public_id))
    member = find_directory_member(public_id)
    if member is None:
        return None
    return find_user_for_member(db, member)


def get_public_user_id(db: Session, user: User) -> str:
    matching_members = [
        member
        for member in load_organization().get("members", [])
        if member.get("name") == user.name
    ]
    matching_users = list(db.scalars(select(User).where(User.name == user.name)))
    if len(matching_members) == 1 and len(matching_users) == 1:
        return str(matching_members[0]["id"])
    return str(user.id)
