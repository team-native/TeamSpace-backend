from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    get_record,
    iso,
    update_record,
    user_can_finance,
    user_profile,
)
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User
from app.services.anonymity import owner_filter
from app.services.security import hash_password, verify_password

router = APIRouter()


class TodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    dueDate: str | None = None
    dueAt: str | None = None
    projectId: str | None = None
    projectName: str | None = None
    category: Literal["개인 업무", "프로젝트 업무", "결재", "일정"] = "개인 업무"
    description: str | None = Field(default=None, max_length=2000)


class TodoPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    dueDate: str | None = None
    dueAt: str | None = None
    projectId: str | None = None
    projectName: str | None = None
    category: Literal["개인 업무", "프로젝트 업무", "결재", "일정"] | None = None
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["미완료", "예정", "완료"] | None = None


def _todo_item(record: Resource) -> dict[str, Any]:
    return {
        "id": str(record.id),
        **record.payload,
        "createdAt": iso(record.created_at),
        "updatedAt": iso(record.updated_at),
    }


def _todo_items(db: Session, user: User) -> list[dict[str, Any]]:
    records = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "todo", Resource.owner_id == user.id)
            .order_by(Resource.id.desc())
        )
    )
    items = [_todo_item(record) for record in records]
    approvals = db.scalars(
        select(Resource)
        .where(Resource.resource_type == "approval")
        .order_by(Resource.id.desc())
    )
    for approval in approvals:
        if approval.payload.get("status") not in {"결재 대기", "검토 중"}:
            continue
        current = next(
            (
                step
                for step in approval.payload.get("line", [])
                if str(step.get("userId")) == str(user.id)
                and step.get("state") == "now"
            ),
            None,
        )
        if current is None:
            continue
        items.append(
            {
                "id": f"approval:{approval.id}",
                "title": f"{approval.payload.get('title', '결재 요청')} 검토",
                "status": "미완료",
                "category": "결재",
                "projectId": approval.payload.get("projectId"),
                "projectName": approval.payload.get("project", ""),
                "sourceType": "approval",
                "sourceId": str(approval.id),
                "href": f"/inhouse/requests/{approval.id}",
                "createdAt": iso(approval.created_at),
                "updatedAt": iso(approval.updated_at),
            }
        )
    items.sort(
        key=lambda item: (
            item.get("dueDate") or "9999-12-31",
            item.get("createdAt") or "",
            item["id"],
        )
    )
    return items


@router.get("/todos", summary="내 할 일 목록")
@router.get("/me/todos", summary="내 할 일 목록", include_in_schema=False)
def get_todos(
    status: str | None = None,
    date_value: str | None = Query(None, alias="date"),
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if status and status not in {"미완료", "예정", "완료"}:
        raise HTTPException(400, "할 일 상태가 올바르지 않습니다.")
    if date_value:
        try:
            date.fromisoformat(date_value)
        except ValueError:
            raise HTTPException(400, "date는 YYYY-MM-DD 형식이어야 합니다.") from None
    items = _todo_items(db, user)
    matching = [
        item
        for item in items
        if (not status or item.get("status", "미완료") == status)
        and (not date_value or item.get("dueDate") == date_value)
    ]
    try:
        offset = int(cursor or 0)
    except ValueError:
        raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    if offset < 0 or offset > len(matching):
        raise HTTPException(400, "cursor 값이 현재 할 일 목록 범위를 벗어났습니다.")
    page = matching[offset : offset + limit]
    return envelope(
        {
            "items": page,
            "nextCursor": str(offset + limit)
            if offset + limit < len(matching)
            else None,
        }
    )


@router.get("/workspace/todos", summary="내 할 일 목록 호환 조회")
def get_workspace_todos(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return envelope({"list": _todo_items(db, user)[:100]})


@router.post("/todos", status_code=201, summary="할 일 추가")
@router.post(
    "/me/todos", status_code=201, summary="할 일 추가", include_in_schema=False
)
def create_todo(
    payload: TodoCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump()
    if data["dueDate"]:
        try:
            date.fromisoformat(data["dueDate"])
        except ValueError:
            raise HTTPException(
                422, "dueDate는 YYYY-MM-DD 형식이어야 합니다."
            ) from None
    data.update(
        status="예정"
        if data["dueDate"] and data["dueDate"] > date.today().isoformat()
        else "미완료",
        completedAt=None,
    )
    record = create_record(db, "todo", data, user)
    return envelope(_todo_item(record), "할 일을 추가했습니다.", 201)


@router.patch("/todos/{todo_id}", summary="할 일 수정 또는 완료 처리")
@router.patch(
    "/me/todos/{todo_id}", summary="할 일 수정 또는 완료 처리", include_in_schema=False
)
def patch_todo(
    todo_id: str,
    payload: TodoPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    todo = get_record(db, "todo", todo_id)
    if todo.owner_id != user.id:
        raise HTTPException(404, "할 일을 찾을 수 없습니다.")
    data = {**todo.payload, **payload.model_dump(exclude_unset=True)}
    if data.get("dueDate"):
        try:
            date.fromisoformat(data["dueDate"])
        except ValueError:
            raise HTTPException(
                422, "dueDate는 YYYY-MM-DD 형식이어야 합니다."
            ) from None
    if data.get("status") == "완료":
        data["completedAt"] = data.get("completedAt") or iso(datetime.now(timezone.utc))
    elif "status" in payload.model_fields_set:
        data["completedAt"] = None
    update_record(db, todo, data)
    return envelope(_todo_item(todo), "할 일을 수정했습니다.")


@router.delete("/todos/{todo_id}", summary="할 일 삭제")
@router.delete("/me/todos/{todo_id}", summary="할 일 삭제", include_in_schema=False)
def delete_todo(
    todo_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    todo = get_record(db, "todo", todo_id)
    if todo.owner_id != user.id:
        raise HTTPException(404, "할 일을 찾을 수 없습니다.")
    db.delete(todo)
    db.commit()
    return envelope({"id": todo_id, "deleted": True}, "할 일을 삭제했습니다.")


@router.get("/projects", summary="프로젝트 목록")
def get_projects(
    q: str | None = None,
    status: str | None = None,
    dept: str | None = None,
    recruiting_only: bool = Query(False, alias="recruitingOnly"),
    role: str | None = None,
    interest_only: bool = Query(False, alias="interestOnly"),
    sort: str = "recent",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if sort not in {"recent", "name", "progress"}:
        raise HTTPException(400, "sort는 recent, name, progress 중 하나여야 합니다.")
    projects = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "project")
            .order_by(Resource.id.desc())
        )
    )
    profile = user_profile(db, user)
    interests = {str(value).casefold() for value in profile.get("interests", [])}
    if profile.get("major"):
        interests.add(str(profile["major"]).casefold())
    values = []
    for project in projects:
        data = project.payload
        project_status = str(data.get("status", "진행 중"))
        department = str(data.get("dept", data.get("department", "")))
        openings = list(data.get("openings", []))
        active_openings = []
        for opening in openings:
            if not isinstance(opening, dict):
                continue
            remaining_value = opening.get(
                "remaining", opening.get("count", opening.get("headcount", 1))
            )
            try:
                remaining = int(remaining_value or 0)
            except (TypeError, ValueError):
                digits = "".join(
                    char for char in str(remaining_value) if char.isdigit()
                )
                remaining = int(digits or "1")
            if (
                not opening.get("closed", False)
                and opening.get("status") not in {"모집 마감", "마감", "완료"}
                and remaining > 0
            ):
                active_openings.append(opening)
        if (
            q
            and q.casefold()
            not in " ".join(
                str(data.get(key, "")) for key in ("name", "description", "desc", "pm")
            ).casefold()
        ):
            continue
        if status and status not in {"전체", "all"} and project_status != status:
            continue
        if dept and dept not in {"전체", "all"} and department != dept:
            continue
        if recruiting_only and not active_openings:
            continue
        selected_openings = active_openings if recruiting_only else openings
        if role and role not in {"전체", "all", "내 관심 직군"}:
            selected_openings = [
                opening
                for opening in selected_openings
                if role.casefold()
                in str(opening.get("role", opening.get("position", ""))).casefold()
            ]
            if not selected_openings:
                continue
        if interest_only or role == "내 관심 직군":
            selected_openings = [
                opening
                for opening in selected_openings
                if str(opening.get("role", opening.get("position", ""))).casefold()
                in interests
            ]
            if not selected_openings:
                continue
        values.append(
            {
                "id": data.get("slug", str(project.id)),
                "name": data.get("name", ""),
                "desc": data.get("description", data.get("desc", "")),
                "role": data.get("role", ""),
                "progress": data.get("progress", 0),
                "status": project_status,
                "tone": data.get("tone", ""),
                "due": data.get("due", ""),
                "milestone": data.get("milestone", ""),
                "nextMilestone": data.get("nextMilestone", data.get("milestone", "")),
                "pm": data.get("pm", ""),
                "dept": department,
                "members": data.get("members", []),
                "openings": selected_openings,
                "updatedAt": data.get("updatedAt") or iso(project.updated_at),
                "_recordId": project.id,
            }
        )
    if sort == "name":
        values.sort(key=lambda item: (item["name"].casefold(), item["_recordId"]))
    elif sort == "progress":
        values.sort(
            key=lambda item: (int(item.get("progress", 0) or 0), item["_recordId"]),
            reverse=True,
        )
    else:
        values.sort(
            key=lambda item: (item["updatedAt"] or "", item["_recordId"]), reverse=True
        )
    start = 0
    if cursor:
        positions = [
            index for index, item in enumerate(values) if str(item["id"]) == cursor
        ]
        if not positions:
            raise HTTPException(400, "cursor가 현재 프로젝트 목록에 없습니다.")
        start = positions[0] + 1
    page = values[start : start + limit]
    next_cursor = str(page[-1]["id"]) if page and start + limit < len(values) else None
    for item in page:
        item.pop("_recordId", None)
    return envelope({"items": page, "nextCursor": next_cursor, "total": len(values)})


@router.get("/me/summary", summary="내 공간 요약")
def my_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    notifications = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "notification", Resource.owner_id == user.id
            )
        )
    )
    approvals = list(
        db.scalars(select(Resource).where(Resource.resource_type == "approval"))
    )
    posts = list(
        db.scalars(
            select(Resource).where(Resource.resource_type == "post", owner_filter(user))
        )
    )
    inquiries = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "inquiry", owner_filter(user)
            )
        )
    )
    proposals = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "proposal", owner_filter(user)
            )
        )
    )
    todos = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "todo", Resource.owner_id == user.id
            )
        )
    )
    mine = [a for a in approvals if a.owner_id == user.id]
    received = [
        a
        for a in approvals
        if any(
            int(s.get("userId", 0) or 0) == user.id and s.get("state") == "now"
            for s in a.payload.get("line", [])
        )
    ]
    return envelope(
        {
            "unreadNotifications": sum(
                bool(n.payload.get("unread", False)) for n in notifications
            ),
            "submittedApprovals": len(mine),
            "myPosts": len(posts),
            "myProposals": len(proposals),
            "myInquiries": len(inquiries),
            "receivedApprovals": len(received),
            "todos": {
                "total": len(todos) + len(received),
                "today": sum(
                    r.payload.get("dueDate") == date.today().isoformat()
                    and r.payload.get("status", "미완료") != "완료"
                    for r in todos
                ),
                "completed": sum(r.payload.get("status") == "완료" for r in todos),
                "linkedApprovals": len(received),
            },
        }
    )


@router.get("/me/activity", summary="내 최근 활동")
def my_activity(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    candidates = list(
        db.scalars(
            select(Resource)
            .where(owner_filter(user))
            .order_by(Resource.id.desc())
            .limit(1000)
        )
    )
    allowed = {
        "approval",
        "post",
        "comment",
        "inquiry",
        "proposal",
        "todo",
        "application",
        "event_rsvp",
    }
    candidates = [r for r in candidates if r.resource_type in allowed]
    if cursor:
        try:
            candidates = [r for r in candidates if r.id < int(cursor)]
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    page = candidates[:limit]
    items = [
        {
            "id": str(r.id),
            "kind": r.resource_type,
            "title": r.payload.get(
                "title", r.payload.get("text", r.payload.get("status", "활동"))
            ),
            "createdAt": iso(r.created_at),
            "href": r.payload.get("href", ""),
        }
        for r in page
    ]
    return envelope(
        {
            "items": items,
            "nextCursor": str(page[-1].id) if len(candidates) > limit else None,
        }
    )


@router.get("/me/projects", summary="내 프로젝트")
def my_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    projects = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "project")
            .order_by(Resource.id.desc())
        )
    )
    ids = {str(user.id), str(user.email)}
    values = []
    for project in projects:
        members = project.payload.get("memberIds", [])
        if (
            user.id in members
            or ids.intersection(map(str, members))
            or project.owner_id == user.id
        ):
            values.append({"id": str(project.id), **project.payload})
    return envelope({"items": values, "nextCursor": None})


@router.get("/projects/{project_id}", summary="프로젝트 상세")
def project_detail(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project", Resource.id == int(project_id)
            )
        )
    except ValueError:
        project = None
    if project is None:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project",
                Resource.payload["slug"].as_string() == project_id,
            )
        )
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    data = project.payload
    member_ids = data.get("memberIds", [])
    members = []
    for member_id in member_ids:
        try:
            member = db.get(User, int(member_id))
        except (ValueError, TypeError):
            member = None
        if member:
            profile = db.scalar(
                select(Resource).where(
                    Resource.resource_type == "profile", Resource.owner_id == member.id
                )
            )
            members.append(
                [
                    member.id.__str__(),
                    (profile.payload if profile else {}).get("major", "Member"),
                ]
            )
    return envelope(
        {
            "id": data.get("slug", str(project.id)),
            "name": data.get("name", ""),
            "desc": data.get("description", data.get("desc", "")),
            "role": data.get("role", ""),
            "progress": data.get("progress", 0),
            "status": data.get("status", "진행 중"),
            "tone": data.get("tone", ""),
            "due": data.get("due", ""),
            "milestone": data.get("milestone", ""),
            "pm": data.get("pm", ""),
            "members": data.get("members", members),
            "openings": data.get("openings", []),
        }
    )


@router.post(
    "/projects/{project_id}/apply", status_code=201, summary="프로젝트 참여 신청"
)
def apply_project(
    project_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project", Resource.id == int(project_id)
            )
        )
    except ValueError:
        project = None
    if project is None:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project",
                Resource.payload["slug"].as_string() == project_id,
            )
        )
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    duplicate = db.scalar(
        select(Resource).where(
            Resource.resource_type == "application",
            Resource.owner_id == user.id,
            Resource.payload["projectId"].as_string() == str(project.id),
        )
    )
    if duplicate:
        raise HTTPException(409, "이미 참여 신청한 프로젝트입니다.")
    if not payload.get("position") and not payload.get("role"):
        raise HTTPException(400, "지원 자리를 선택해주세요.")
    application = create_record(
        db,
        "application",
        {
            **payload,
            "projectId": str(project.id),
            "projectName": project.payload.get("name", ""),
            "status": "APPLIED",
            "createdAt": iso(datetime.now().astimezone()),
        },
        user,
    )
    for member_id in project.payload.get("pmIds", []):
        try:
            notify_user(
                db,
                int(member_id),
                "내 업무",
                f"{user.name}님이 프로젝트 참여를 신청했습니다.",
                f"/inhouse/my/projects/{project_id}",
                True,
            )
        except (TypeError, ValueError):
            pass
    return envelope(
        {"applicationId": str(application.id), "status": "APPLIED"},
        "참여 신청을 접수했습니다.",
        201,
    )


class Rsvp(BaseModel):
    rsvp: str = Field(pattern="^(참석|불참|미정)$")
    reason: str | None = None


@router.get("/events", summary="월별 일정")
def get_events(
    month: str,
    scope: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(400, "month는 YYYY-MM 형식이어야 합니다.")
    if scope and scope not in {
        "전체",
        "팀",
        "프로젝트",
        "개인",
        "all",
        "team",
        "project",
        "personal",
    }:
        raise HTTPException(400, "지원하지 않는 일정 범위입니다.")
    records = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "event")
            .order_by(Resource.id.desc())
        )
    )
    events = []
    for record in records:
        data = record.payload
        if not str(data.get("date", "")).startswith(month):
            continue
        event_scope = data.get("scope", data.get("kind", "팀"))
        if (
            scope
            and scope not in {"전체", "all"}
            and event_scope
            not in {
                scope,
                {"team": "팀", "project": "프로젝트", "personal": "개인"}.get(scope),
            }
        ):
            continue
        if (
            q
            and q.casefold()
            not in " ".join(
                str(data.get(key, "")) for key in ("title", "place", "host", "agenda")
            ).casefold()
        ):
            continue
        event = {
            "id": str(record.id),
            **{k: v for k, v in data.items() if k != "rsvpByUser"},
        }
        event["rsvp"] = bool(data.get("rsvpEnabled", data.get("rsvp", False)))
        responses = data.get("rsvpByUser", {})
        my_rsvp = responses.get(str(user.id))
        event["myRsvp"] = my_rsvp.get("rsvp") if isinstance(my_rsvp, dict) else my_rsvp
        event["rsvpCount"] = sum(
            isinstance(response, dict) and response.get("rsvp") == "참석"
            for response in responses.values()
        )
        events.append(event)
    events.sort(
        key=lambda event: (
            event.get("date", ""),
            event.get("time", ""),
            int(event["id"]),
        )
    )
    start = 0
    if cursor:
        positions = [
            index for index, event in enumerate(events) if event["id"] == cursor
        ]
        if not positions:
            raise HTTPException(400, "cursor가 현재 일정 목록에 없습니다.")
        start = positions[0] + 1
    page = events[start : start + limit]
    return envelope(
        {
            "items": page,
            "nextCursor": page[-1]["id"]
            if page and start + limit < len(events)
            else None,
        }
    )


@router.get("/dashboard/home", summary="홈 대시보드")
def dashboard_home(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    today = date.today().isoformat()
    todos = _todo_items(db, user)
    open_todos = [item for item in todos if item.get("status", "미완료") != "완료"]
    today_todos = [todo for todo in open_todos if todo.get("dueDate") == today]
    notifications = list(
        db.scalars(
            select(Resource)
            .where(
                Resource.resource_type == "notification",
                Resource.owner_id == user.id,
            )
            .order_by(Resource.id.desc())
            .limit(5)
        )
    )
    unread_count = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "notification",
                Resource.owner_id == user.id,
                Resource.payload["unread"].as_boolean().is_(True),
            )
        )
        or 0
    )
    approvals = list(
        db.scalars(select(Resource).where(Resource.resource_type == "approval"))
    )
    pending_approvals = [
        {"id": str(approval.id), **approval.payload}
        for approval in approvals
        if any(
            str(line.get("userId")) == str(user.id) and line.get("state") == "now"
            for line in approval.payload.get("line", [])
        )
    ]
    next_events = [
        record
        for record in db.scalars(
            select(Resource).where(Resource.resource_type == "event")
        )
        if str(record.payload.get("date", "")) >= today
    ]
    next_events.sort(
        key=lambda record: (
            record.payload.get("date", ""),
            record.payload.get("time", ""),
        )
    )
    event_preview = (
        {"id": str(next_events[0].id), **next_events[0].payload}
        if next_events
        else None
    )
    latest_notice = db.scalar(
        select(Resource)
        .where(Resource.resource_type == "notice")
        .order_by(Resource.id.desc())
        .limit(1)
    )
    notice_preview = (
        {"id": str(latest_notice.id), **latest_notice.payload}
        if latest_notice
        else None
    )
    data = {
        "me": user_profile(db, user),
        "summary": {
            "todoCount": len(open_todos),
            "todayTodoCount": len(today_todos),
            "unreadNotifications": int(unread_count or 0),
            "receivedApprovalCount": len(pending_approvals),
            "notice": notice_preview,
            "schedule": event_preview,
        },
        "todos": open_todos[:5],
        "notifications": [
            {"id": str(record.id), **record.payload} for record in notifications
        ],
        "approvals": pending_approvals[:5],
        "events": [
            {"id": str(record.id), **record.payload} for record in next_events[:5]
        ],
        "projects": my_projects(db, user)["items"][:5],
    }
    if user_can_finance(user):
        finance_settings = db.scalar(
            select(Resource).where(Resource.resource_type == "finance_settings")
        )
        pools = (finance_settings.payload if finance_settings else {}).get("pools", [])
        data["finance"] = {
            "available": sum(
                int(pool.get("amount", 0) or 0)
                for pool in pools
                if pool.get("id") == "operation"
            ),
            "pools": pools,
        }
    return envelope(data)


@router.post("/events/{event_id}/rsvp", summary="일정 참석 응답")
def rsvp(
    event_id: str,
    payload: Rsvp,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.api.shared import get_record

    event = get_record(db, "event", event_id)
    replies = dict(event.payload.get("rsvpByUser", {}))
    replies[str(user.id)] = {
        "rsvp": payload.rsvp,
        "reason": payload.reason,
        "at": iso(datetime.now().astimezone()),
    }
    event.payload = {**event.payload, "rsvpByUser": replies}
    db.commit()
    db.add(
        Resource(
            resource_type="event_rsvp",
            owner_id=user.id,
            payload={
                "eventId": event_id,
                "title": event.payload.get("title"),
                "rsvp": payload.rsvp,
            },
        )
    )
    db.commit()
    return envelope(
        {"eventId": event_id, "rsvp": payload.rsvp}, "참석 응답을 저장했습니다."
    )


@router.get("/me/settings", summary="내 설정 조회")
def get_settings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "user_settings", Resource.owner_id == user.id
        )
    )
    return envelope(record.payload if record else {"theme": "시스템"})


@router.patch("/me/settings", summary="내 설정 수정")
def patch_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    values = dict(payload)
    current_password = values.pop("currentPassword", None)
    new_password = values.pop("newPassword", None)
    if new_password:
        if not current_password or not verify_password(
            current_password, user.password_hash
        ):
            raise HTTPException(400, "현재 비밀번호가 일치하지 않습니다.")
        if len(new_password) < 8:
            raise HTTPException(422, "새 비밀번호는 8자 이상이어야 합니다.")
        user.password_hash = hash_password(new_password)
    theme = values.get("theme")
    if theme and theme not in {"시스템", "라이트", "다크"}:
        raise HTTPException(422, "지원하지 않는 테마입니다.")
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "user_settings", Resource.owner_id == user.id
        )
    )
    current = record.payload if record else {}
    updated = {**current, **values}
    if record:
        update_record(db, record, updated)
    else:
        create_record(db, "user_settings", updated, user)
    db.commit()
    return envelope(updated, "설정을 저장했습니다.")


def notify_user(
    db: Session,
    user_id: int | None,
    kind: str,
    title: str,
    href: str,
    action: bool = False,
):
    if user_id is None:
        return
    from app.api.shared import notify_user as create_notification

    create_notification(db, user_id, kind, title, href, action)
