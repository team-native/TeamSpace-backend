import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.resource import Resource
from app.models.user import User

ORGANIZATION_PATH = Path(__file__).resolve().parents[1] / "data" / "organization.json"
MEMBER_ID_PATTERN = re.compile(r"^member-(\d+)$")


def load_organization() -> dict[str, Any]:
    with ORGANIZATION_PATH.open(encoding="utf-8") as organization_file:
        return json.load(organization_file)


def is_admin_account(user: User) -> bool:
    roles = {str(role).strip().casefold() for role in (user.roles or [])}
    dept = str(user.department or "").strip().casefold()
    return "admin" in roles or dept == "admin"


def normalize_department_name(
    department: str | None, groups: list[dict[str, Any]] | None = None
) -> str:
    raw = str(department or "").strip()
    if not raw:
        return ""
    group_list = (
        groups
        if groups is not None
        else load_organization().get("organizationChart", {}).get("groups", [])
    )
    for group in group_list:
        group_name = str(group.get("name") or "").strip()
        if group_name and group_name.casefold() == raw.casefold():
            return group_name
    for canonical in (
        "NativeOps",
        "FA",
        "NativeLab",
        "IT",
        "iOS",
        "Android",
        "Flutter",
        "Frontend",
        "Backend",
        "DevOps",
        "Design & Client",
    ):
        if canonical.casefold() == raw.casefold():
            return canonical
    return raw


def next_generated_member_id(
    directory_members: list[dict[str, Any]], assigned_ids: set[str]
) -> str:
    max_num = 0
    for member in directory_members:
        match = MEMBER_ID_PATTERN.match(str(member.get("id", "")))
        if match:
            max_num = max(max_num, int(match.group(1)))
    for member_id in assigned_ids:
        match = MEMBER_ID_PATTERN.match(str(member_id))
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"member-{max_num + 1:02d}"


def build_user_member_id_map(
    db: Session,
    directory_members: list[dict[str, Any]] | None = None,
) -> dict[int, str]:
    if directory_members is None:
        directory_members = load_organization().get("members", [])
    users = list(db.scalars(select(User).order_by(User.id)))
    profiles = {
        profile.owner_id: (profile.payload or {})
        for profile in db.scalars(
            select(Resource).where(Resource.resource_type == "profile")
        )
    }

    directory_ids = {str(m.get("id")) for m in directory_members}
    assigned: dict[int, str] = {}
    used_ids: set[str] = set(directory_ids)

    for user in users:
        if is_admin_account(user):
            continue
        explicit_id = str(profiles.get(user.id, {}).get("organizationMemberId") or "")
        if explicit_id and explicit_id in directory_ids:
            assigned[user.id] = explicit_id
            used_ids.add(explicit_id)

    for user in users:
        if is_admin_account(user) or user.id in assigned:
            continue
        new_id = next_generated_member_id(directory_members, used_ids)
        assigned[user.id] = new_id
        used_ids.add(new_id)

    return assigned


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
    if member is not None:
        return find_user_for_member(db, member)
    member_map = build_user_member_id_map(db)
    for uid, mapped_id in member_map.items():
        if mapped_id == public_id:
            return db.get(User, uid)
    return None


def get_public_user_id(db: Session, user: User) -> str:
    member_id = _profile_member_id(db, user.id)
    if member_id and find_directory_member(member_id):
        return str(member_id)
    member_map = build_user_member_id_map(db)
    if user.id in member_map:
        return member_map[user.id]
    return str(user.id)