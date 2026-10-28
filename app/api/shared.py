from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.resource import Resource
from app.models.user import User


def envelope(data: Any, message: str = "조회 성공", code: int = 200) -> dict[str, Any]:
    # The in-house frontend consumes the typed payload directly (e.g. {items, nextCursor}).
    return data


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def create_record(
    db: Session, kind: str, payload: dict[str, Any], owner: User | None = None
) -> Resource:
    record = Resource(
        resource_type=kind, payload=payload, owner_id=owner.id if owner else None
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def update_record(db: Session, record: Resource, payload: dict[str, Any]) -> Resource:
    record.payload = payload
    db.commit()
    db.refresh(record)
    return record


def get_record(db: Session, kind: str, record_id: str | int) -> Resource:
    try:
        numeric_id = int(record_id)
    except (ValueError, TypeError):
        raise HTTPException(404, "요청한 정보를 찾을 수 없습니다.") from None
    record = db.scalar(
        select(Resource).where(
            Resource.id == numeric_id, Resource.resource_type == kind
        )
    )
    if record is None:
        raise HTTPException(404, "요청한 정보를 찾을 수 없습니다.")
    return record


def list_records(
    db: Session, kind: str, cursor: str | None, limit: int = 20
) -> tuple[list[Resource], str | None]:
    stmt = (
        select(Resource)
        .where(Resource.resource_type == kind)
        .order_by(Resource.id.desc())
    )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.limit(limit + 1)))
    next_cursor = str(records[limit - 1].id) if len(records) > limit else None
    return records[:limit], next_cursor


def item(record: Resource) -> dict[str, Any]:
    return {"id": str(record.id), **(record.payload or {})}


def total_records(db: Session, kind: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(Resource.resource_type == kind)
        )
        or 0
    )


def user_profile(
    db: Session,
    user: User,
    profile_data: dict[str, Any] | None = None,
    projects_by_user: dict[str, list[list[str]]] | None = None,
) -> dict[str, Any]:
    joined_at = user.created_at
    if joined_at and joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)
    if profile_data is None:
        profile = db.scalar(
            select(Resource).where(
                Resource.resource_type == "profile", Resource.owner_id == user.id
            )
        )
        data = profile.payload if profile else {}
    else:
        data = profile_data
    projects = data.get("projects")
    if projects is None:
        if projects_by_user is not None:
            projects = projects_by_user.get(str(user.id), [])
        else:
            projects = []
            for project in db.scalars(
                select(Resource).where(Resource.resource_type == "project")
            ):
                member_ids = {
                    str(member_id) for member_id in project.payload.get("memberIds", [])
                }
                if str(user.id) in member_ids:
                    projects.append(
                        [
                            project.payload.get("name", ""),
                            project.payload.get("role", "Member"),
                        ]
                    )
    return {
        "id": str(user.id),
        "name": user.name,
        "email": user.email,
        "dept": data.get("dept", user.department or ""),
        "role": data.get("major", ""),
        "major": data.get("major", ""),
        "cohort": data.get("cohort", ""),
        "github": data.get("github", ""),
        "photo": data.get("photo", ""),
        "joined": iso(joined_at),
        "bio": data.get("bio", ""),
        "interests": data.get("interests", []),
        "projects": projects,
        "isNew": bool(joined_at and (utc_now() - joined_at).days < 7),
        "lead": bool(
            set(user.roles or []).intersection({"Leader", "Admin", "Lead", "부서 리드"})
        ),
        "roles": user.roles or [],
    }


def user_can_finance(user: User) -> bool:
    return bool(set(user.roles or []).intersection({"Admin", "FA", "Leader"}))


def require_finance(user: User) -> None:
    if not user_can_finance(user):
        raise HTTPException(403, "재정 정보 열람 권한이 없습니다.")


def notify_user(
    db: Session,
    user_id: int | None,
    kind: str,
    title: str,
    href: str,
    action: bool = False,
) -> None:
    if user_id is None:
        return
    preferences = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notification_settings",
            Resource.owner_id == user_id,
        )
    )
    notification_kinds = (preferences.payload.get("kinds") or {}) if preferences else {}
    if notification_kinds.get(kind, True) is False:
        return
    valid_kinds = {"내 업무", "재정", "일정", "공지"}
    notification = {
        "kind": kind if kind in valid_kinds else "공지",
        "sourceKind": kind,
        "title": title,
        "createdAt": iso(utc_now()),
        "href": href,
        "unread": True,
        "action": action,
    }
    create_record(db, "notification", notification, db.get(User, user_id))
    preference_data = preferences.payload if preferences else {}
    if preference_data.get("push", True) is not False and not _is_do_not_disturb(
        preference_data
    ):
        from app.services.push import send_push

        send_push(db, user_id, notification, preference_data)


def _is_do_not_disturb(preferences: dict[str, Any]) -> bool:
    dnd_value = preferences.get("dnd", False)
    dnd_settings = dnd_value if isinstance(dnd_value, dict) else {}
    if not dnd_settings.get("enabled", bool(dnd_value)):
        return False
    try:
        local_now = datetime.now(ZoneInfo(settings.app_timezone))
        start = time.fromisoformat(
            str(preferences.get("dndFrom", dnd_settings.get("from", "23:00")))
        )
        end = time.fromisoformat(
            str(preferences.get("dndTo", dnd_settings.get("to", "08:00")))
        )
    except (ValueError, KeyError):
        return False
    configured_days = preferences.get("dndDays", dnd_settings.get("days", [])) or []
    if isinstance(configured_days, str):
        configured_days = [configured_days]
    days = set(configured_days)
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    selected_today = (
        not days
        or weekday_names[local_now.weekday()] in days
        or str(local_now.weekday()) in days
    )
    current = local_now.time().replace(tzinfo=None)
    if start == end:
        return selected_today
    if start < end:
        return selected_today and start <= current < end
    if current >= start:
        return selected_today
    previous_day = weekday_names[(local_now.weekday() - 1) % 7]
    previous_index = str((local_now.weekday() - 1) % 7)
    selected_previous = not days or previous_day in days or previous_index in days
    return selected_previous and current < end
