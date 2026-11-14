import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    iso,
    notification_response,
    notify_user,
    update_record,
    user_profile,
)
from app.core.config import settings
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User
from app.services.email import EmailConfigurationError, EmailDeliveryError, send_email
from app.services.organization import (
    build_user_member_id_map,
    get_user_by_public_id,
    is_admin_account,
    load_organization,
    normalize_department_name,
)
from app.services.security import hash_password

router = APIRouter()
logger = logging.getLogger(__name__)
CARD_ORGANIZATIONS = {"NativeLab", "NativeOps", "FA", "IT"}


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    dept: str | None = None
    department: str | None = None
    major: str | None = None
    cohort: str | None = None
    github: str | None = None
    photo: str | None = None
    avatarBackground: Literal["GRAY", "BLUE", "YELLOW", "GREEN", "PINK"] | None = None
    bio: str | None = Field(default=None, max_length=60)
    interests: list[str] | None = None


class SignupRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    dept: str
    password: str | None = Field(default=None, min_length=8, max_length=128)
    major: str | None = None
    cohort: str | None = None
    message: str | None = None


class ResetRequest(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class ReadNotifications(BaseModel):
    id: str | None = None
    all: bool = False


class NotificationSettingsPatch(BaseModel):
    push: bool | None = None
    kinds: dict[str, bool] | None = None
    dnd: bool | None = None
    dndFrom: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    dndTo: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    dndDays: list[str] | None = None


@router.get("/me", summary="내 정보")
def get_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return envelope(user_profile(db, user))


@router.patch("/me", summary="프로필 수정")
def patch_me(
    payload: ProfilePatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump(exclude_unset=True)
    if "department" in data:
        data["dept"] = data.pop("department")
    if "name" in data and data["name"] is not None:
        user.name = data.pop("name")
    if "dept" in data and data["dept"] is not None:
        normalized_dept = normalize_department_name(data["dept"])
        data["dept"] = normalized_dept
        user.department = normalized_dept
    if (
        "interests" in data
        and data["interests"] is not None
        and len(data["interests"]) > 5
    ):
        raise HTTPException(422, "관심사는 최대 5개까지 등록할 수 있습니다.")
    profile = db.scalar(
        select(Resource).where(
            Resource.resource_type == "profile", Resource.owner_id == user.id
        )
    )
    current = profile.payload if profile else {}
    if profile:
        profile.payload = {**current, **data}
    else:
        db.add(Resource(resource_type="profile", owner_id=user.id, payload=data))
    db.commit()
    db.refresh(user)
    return envelope(user_profile(db, user), "프로필을 저장했습니다.")


@router.get("/people", summary="팀원 목록")
def get_people(
    q: str | None = None,
    dept: str | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    people = _people_items(db)
    if dept:
        normalized_dept = normalize_department_name(dept)
        people = [
            person
            for person in people
            if normalize_department_name(person.get("dept")) == normalized_dept
        ]
    if q:
        query = q.strip().casefold()
        people = [
            person
            for person in people
            if any(
                query in str(person.get(field) or "").casefold()
                for field in ("name", "email", "dept", "role", "bio")
            )
        ]
    if cursor:
        cursor_index = next(
            (index for index, person in enumerate(people) if person["id"] == cursor),
            None,
        )
        if cursor_index is None:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.")
        people = people[cursor_index + 1 :]
    page = people[:limit]
    next_cursor = page[-1]["id"] if len(people) > limit and page else None
    return envelope({"items": page, "nextCursor": next_cursor})


def _people_items(db: Session) -> list[dict[str, Any]]:
    org_data = load_organization()
    directory = org_data.get("members", [])
    groups = org_data.get("organizationChart", {}).get("groups", [])
    # 활성 상태이면서 Admin이 아닌 계정만 조회 (비활성화/삭제된 계정은 목록에서 제외)
    users = [
        u
        for u in db.scalars(select(User).where(User.active.is_(True)).order_by(User.id))
        if not is_admin_account(u)
    ]
    profiles = {
        profile.owner_id: (profile.payload or {})
        for profile in db.scalars(
            select(Resource).where(Resource.resource_type == "profile")
        )
    }
    member_id_map = build_user_member_id_map(db, directory)
    users_by_member_id: dict[str, list[User]] = {}
    for person in users:
        member_id = profiles.get(person.id, {}).get("organizationMemberId")
        if member_id:
            users_by_member_id.setdefault(str(member_id), []).append(person)
    projects_by_user: dict[str, list[list[str]]] = {}
    for project in db.scalars(
        select(Resource).where(Resource.resource_type == "project")
    ):
        for member_id in project.payload.get("memberIds", []):
            projects_by_user.setdefault(str(member_id), []).append(
                [project.payload.get("name", ""), project.payload.get("role", "Member")]
            )

    items = []
    linked_user_ids = set()
    for member in directory:
        mapped_matches = users_by_member_id.get(member["id"], [])
        if len(mapped_matches) > 1:
            raise HTTPException(409, "조직도 멤버에 여러 계정이 연결되어 있습니다.")
        person = mapped_matches[0] if mapped_matches else None
        if person is None:
            raw_department = normalize_department_name(
                str(member.get("primaryDepartment", "") or ""), groups
            )
            card_department = (
                raw_department
                if raw_department in CARD_ORGANIZATIONS
                else str(member.get("primaryOrg", "") or "")
                if member.get("primaryOrg") in CARD_ORGANIZATIONS
                else raw_department
            )
            major = str(
                member.get("major")
                or next(
                    (
                        department
                        for department in member.get("departments", [])
                        if department not in CARD_ORGANIZATIONS
                    ),
                    raw_department
                    if raw_department not in CARD_ORGANIZATIONS
                    else "",
                )
            )
            projects = [
                [entry.get("project", ""), entry.get("role", "Member")]
                for entry in member.get("projects", [])
            ]
            items.append(
                {
                    "id": member["id"],
                    "href": f"/inhouse/people/{member['id']}",
                    "name": member.get("name", ""),
                    "email": "",
                    "dept": card_department,
                    "department": raw_department,
                    "role": major,
                    "major": major,
                    "displaySubtitle": _people_subtitle(card_department, major),
                    "cohort": "",
                    "github": "",
                    "photo": "",
                    "avatarBackground": "",
                    "joined": "",
                    "bio": "",
                    "interests": [],
                    "projects": projects,
                    "isNew": bool(member.get("isNew", False)),
                    "lead": bool(member.get("isLead", False)),
                    "roles": ["Member"],
                    "active": True,
                    "linked": False,
                    "inOrganization": True,
                    "userId": None,
                }
            )
            continue
        linked_user_ids.add(person.id)
        item = user_profile(db, person, profiles.get(person.id, {}), projects_by_user)
        item["id"] = member["id"]
        item["href"] = f"/inhouse/people/{member['id']}"
        item["linked"] = True
        item["inOrganization"] = True
        item["userId"] = str(person.id)
        raw_department = normalize_department_name(
            person.department
            or profiles.get(person.id, {}).get(
                "dept", member.get("primaryDepartment", "")
            ),
            groups,
        )
        major = str(
            profiles.get(person.id, {}).get("major")
            or next(
                (
                    department
                    for department in member.get("departments", [])
                    if department not in CARD_ORGANIZATIONS
                ),
                "",
            )
        )
        item["department"] = raw_department
        item["dept"] = raw_department
        item["role"] = major
        item["major"] = major
        item["displaySubtitle"] = _people_subtitle(item["dept"], major)
        if not item.get("projects"):
            item["projects"] = [
                [entry.get("project", ""), entry.get("role", "Member")]
                for entry in member.get("projects", [])
            ]
        items.append(item)

    for person in users:
        if person.id not in linked_user_ids:
            public_id = member_id_map[person.id]
            item = user_profile(
                db, person, profiles.get(person.id, {}), projects_by_user
            )
            raw_department = normalize_department_name(
                person.department or profiles.get(person.id, {}).get("dept", ""),
                groups,
            )
            major = str(profiles.get(person.id, {}).get("major") or "")
            item["id"] = public_id
            item["href"] = f"/inhouse/people/{public_id}"
            item["department"] = raw_department
            item["dept"] = raw_department
            item["role"] = major or raw_department
            item["major"] = major
            item["displaySubtitle"] = _people_subtitle(raw_department, major)
            item["linked"] = True
            item["inOrganization"] = False
            item["userId"] = str(person.id)
            items.append(item)
    return items


def _people_subtitle(department: str, major: str) -> str:
    if department and major:
        return f"{department} · {major}"
    return department or major


@router.get("/people/{person_id}", summary="팀원 상세")
def get_person(
    person_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    people = _people_items(db)
    item = next((person for person in people if person["id"] == person_id), None)
    if item:
        return envelope(item)
    person = get_user_by_public_id(db, person_id)
    if person is None or is_admin_account(person) or not person.active:
        raise HTTPException(404, "팀원을 찾을 수 없습니다.")
    mapped = next(
        (entry for entry in people if entry.get("userId") == str(person.id)),
        None,
    )
    if mapped:
        return envelope(mapped)
    return envelope(user_profile(db, person))


@router.post("/auth/signup-requests", status_code=202, summary="가입 요청")
def signup_request(payload: SignupRequest, db: Session = Depends(get_db)):
    existing = db.scalar(select(User.id).where(User.email == str(payload.email)))
    if existing:
        raise HTTPException(409, "이미 등록된 이메일입니다.")
    pending = db.scalar(
        select(Resource).where(
            Resource.resource_type == "signup_request",
            Resource.payload["email"].as_string() == str(payload.email),
            Resource.payload["status"].as_string() == "PENDING",
        )
    )
    if pending:
        raise HTTPException(409, "이미 검토 중인 가입 요청이 있습니다.")
    data = payload.model_dump(exclude={"password"})
    data["email"] = str(payload.email)
    data["dept"] = normalize_department_name(payload.dept)
    if payload.password:
        data["passwordHash"] = hash_password(payload.password)
    data.update(
        status="PENDING",
        createdAt=iso(datetime.now(timezone.utc)),
    )
    record = create_record(db, "signup_request", data)
    for admin in db.scalars(select(User).order_by(User.id)):
        if "admin" in {str(role).strip().casefold() for role in admin.roles or []}:
            notify_user(
                db,
                admin.id,
                "내 업무",
                "새 가입 요청이 접수됐습니다.",
                "/inhouse/admin/signups",
                action=True,
                message=f"{payload.name} · {payload.email}",
                notification_type="SIGNUP_REQUEST",
            )
    return envelope(
        {"id": str(record.id), "status": "PENDING"}, "가입 요청을 접수했습니다.", 202
    )


def send_reset_email(email: str, token: str) -> None:
    reset_url = f"{settings.frontend_base_url}/inhouse/reset-password?token={token}"
    try:
        send_email(
            to_email=email,
            subject="TeamSpace 비밀번호 재설정",
            text_body=(
                f"비밀번호 재설정 링크: {reset_url}\n"
                "요청하지 않으셨다면 이 메일을 무시하세요."
            ),
        )
    except EmailConfigurationError as exc:
        raise HTTPException(
            503, "비밀번호 재설정 메일을 보낼 SMTP 설정이 필요합니다."
        ) from exc
    except EmailDeliveryError as exc:
        raise HTTPException(
            502, "SMTP 서버에서 메일을 보내지 못했습니다. 설정을 확인해주세요."
        ) from exc


def _deliver_reset_email(email: str, token: str) -> None:
    try:
        send_reset_email(email, token)
    except HTTPException as exc:
        logger.warning(
            "Password reset email delivery failed with HTTP status %s",
            exc.status_code,
        )


@router.post(
    "/auth/password-reset-requests",
    status_code=202,
    summary="비밀번호 재설정 메일 요청",
)
def password_reset_request(
    payload: ResetRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    email = str(payload.email).strip().casefold()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    # Avoid exposing whether an account exists.
    if user is None:
        return envelope({"accepted": True}, "요청을 처리했습니다.", 202)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    create_record(
        db,
        "password_reset",
        {
            "userId": user.id,
            "tokenHash": token_hash,
            "expiresAt": iso(datetime.now(timezone.utc) + timedelta(minutes=30)),
            "used": False,
        },
    )
    background_tasks.add_task(_deliver_reset_email, user.email, token)
    return envelope({"accepted": True}, "요청을 처리했습니다.", 202)


@router.post("/auth/password-reset", summary="비밀번호 재설정")
def password_reset(payload: ResetPassword, db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    request = db.scalar(
        select(Resource).where(
            Resource.resource_type == "password_reset",
            Resource.payload["tokenHash"].as_string() == token_hash,
            Resource.payload["used"].as_boolean().is_(False),
        )
    )
    if request is None or datetime.fromisoformat(
        request.payload["expiresAt"]
    ) < datetime.now(timezone.utc):
        raise HTTPException(400, "재설정 링크가 유효하지 않거나 만료되었습니다.")
    user = db.get(User, request.payload["userId"])
    if user is None:
        raise HTTPException(400, "재설정 요청을 확인할 수 없습니다.")
    user.password_hash = hash_password(payload.password)
    request.payload = {**request.payload, "used": True}
    db.commit()
    return envelope({"success": True}, "비밀번호를 변경했습니다.")


@router.get("/notifications", summary="알림 목록")
def get_notifications(
    kind: str | None = None,
    unread: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = (
        select(Resource)
        .where(Resource.resource_type == "notification", Resource.owner_id == user.id)
        .order_by(Resource.id.desc())
    )
    if kind and kind not in {"전체", "all"}:
        stmt = stmt.where(
            or_(
                Resource.payload["kind"].as_string() == kind,
                Resource.payload["sourceKind"].as_string() == kind,
                Resource.payload["type"].as_string() == kind,
            )
        )
    if unread is not None:
        stmt = stmt.where(
            or_(
                Resource.payload["unread"].as_boolean().is_(unread),
                and_(
                    Resource.payload["unread"].as_boolean().is_(None),
                    Resource.payload["isRead"].as_boolean().is_(not unread),
                ),
            )
        )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.limit(limit + 1)))
    next_cursor = str(records[limit - 1].id) if len(records) > limit else None
    return envelope(
        {
            "items": [notification_response(r) for r in records[:limit]],
            "nextCursor": next_cursor,
        }
    )


@router.post("/notifications/read", summary="알림 읽음 처리")
def read_notifications(
    payload: ReadNotifications,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not payload.id and not payload.all:
        raise HTTPException(400, "id 또는 all=true를 지정해주세요.")
    stmt = select(Resource).where(
        Resource.resource_type == "notification", Resource.owner_id == user.id
    )
    if payload.id and not payload.all:
        try:
            stmt = stmt.where(Resource.id == int(payload.id))
        except ValueError:
            raise HTTPException(400, "알림 ID가 올바르지 않습니다.") from None
    records = list(db.scalars(stmt))
    for record in records:
        record.payload = {**record.payload, "unread": False}
    db.commit()
    return envelope({"updated": len(records)}, "읽음 처리했습니다.")


@router.get("/me/notification-settings", summary="알림 설정 조회")
def get_notification_settings(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notification_settings",
            Resource.owner_id == user.id,
        )
    )
    default_kinds = {
        "내 업무": True,
        "재정": True,
        "일정": True,
        "공지": True,
        "규칙 (제도 변경)": True,
        "게시판": False,
    }
    defaults: dict[str, Any] = {
        "push": False,
        "kinds": default_kinds,
        "dnd": True,
        "dndFrom": "23:00",
        "dndTo": "08:00",
        "dndDays": ["월", "화", "수", "목", "금", "토", "일"],
    }
    if not record or not record.payload:
        return envelope(defaults)
    merged = {**defaults, **record.payload}
    merged["push"] = bool(merged.get("push", False))
    merged["kinds"] = {**default_kinds, **(record.payload.get("kinds") or {})}
    if merged.get("dndDays") is None:
        merged["dndDays"] = defaults["dndDays"]
    return envelope(merged)


@router.patch("/me/notification-settings", summary="알림 설정 수정")
def patch_notification_settings(
    payload: NotificationSettingsPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notification_settings",
            Resource.owner_id == user.id,
        )
    )
    current = record.payload if record else {}
    values = payload.model_dump(exclude_unset=True)
    if "kinds" in values and values["kinds"] is not None:
        allowed_kinds = {
            "내 업무",
            "재정",
            "일정",
            "공지",
            "규칙 (제도 변경)",
            "게시판",
        }
        if not set(values["kinds"]).issubset(allowed_kinds):
            raise HTTPException(422, "지원하지 않는 알림 분류가 포함되어 있습니다.")
        values["kinds"] = {**current.get("kinds", {}), **values["kinds"]}
    if "dndDays" in values and values["dndDays"] is not None:
        allowed_days = {"월", "화", "수", "목", "금", "토", "일"}
        if not set(values["dndDays"]).issubset(allowed_days):
            raise HTTPException(422, "방해 금지 요일이 올바르지 않습니다.")
    data = {**current, **values}
    if record:
        update_record(db, record, data)
    else:
        create_record(db, "notification_settings", data, user)
    return envelope(data, "알림 설정을 저장했습니다.")


@router.post("/push/subscriptions", status_code=201, summary="브라우저 푸시 구독 등록")
def add_push_subscription(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    endpoint = payload.get("endpoint")
    if not endpoint or "keys" not in payload:
        raise HTTPException(400, "유효한 Push 구독 정보가 필요합니다.")
    current = db.scalar(
        select(Resource).where(
            Resource.resource_type == "push_subscription",
            Resource.owner_id == user.id,
            Resource.payload["endpoint"].as_string() == endpoint,
        )
    )
    if current:
        update_record(db, current, payload)
    else:
        create_record(db, "push_subscription", payload, user)
    return envelope({"success": True}, "푸시 구독을 저장했습니다.", 201)


@router.get("/push/vapid-public-key", summary="브라우저 푸시 공개 키")
def get_vapid_public_key():
    if not settings.vapid_public_key:
        raise HTTPException(503, "브라우저 푸시 VAPID 설정이 필요합니다.")
    return envelope({"publicKey": settings.vapid_public_key})


@router.delete("/push/subscriptions", summary="브라우저 푸시 구독 해제")
def delete_push_subscription(
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    endpoint = payload.get("endpoint", "")
    subscription = db.scalar(
        select(Resource).where(
            Resource.resource_type == "push_subscription",
            Resource.owner_id == user.id,
            Resource.payload["endpoint"].as_string() == endpoint,
        )
    )
    if subscription:
        db.delete(subscription)
        db.commit()
    return envelope({"success": True}, "푸시 구독을 해제했습니다.")