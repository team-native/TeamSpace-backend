import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    get_record,
    iso,
    list_records,
    notify_user,
    update_record,
    user_can_finance,
)
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models.resource import Resource
from app.models.user import User
from app.services.anonymity import (
    is_record_owner,
    public_content_payload,
    resolve_anonymous_owner,
)
from app.services.email import send_email

router = APIRouter()
UPLOAD_DIR = Path(__file__).resolve().parents[2] / "uploads"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


def _send_signup_approved_email(email: str, name: str) -> None:
    try:
        send_email(
            to_email=email,
            subject="TeamSpace 계정 요청 승인 안내",
            text_body=(
                f"{name}님, TeamSpace 계정 요청이 승인되었습니다.\n"
                f"로그인: {settings.frontend_base_url}/inhouse/login"
            ),
        )
    except Exception:
        logger.exception("Could not send account approval email to %s", email)


def _upload_reference_ids(value: Any) -> set[str]:
    if isinstance(value, (list, tuple)):
        return (
            set().union(*(_upload_reference_ids(item) for item in value))
            if value
            else set()
        )
    if isinstance(value, dict):
        result = set()
        for key in ("id", "uploadId", "fileId", "url", "downloadUrl"):
            if key in value:
                result.update(_upload_reference_ids(value[key]))
        return result
    text = str(value or "")
    if "/uploads/" in text:
        text = text.split("/uploads/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    return {text} if text.isdigit() else set()


def _can_download_upload(db: Session, upload: Resource, user: User) -> bool:
    if is_record_owner(upload, user):
        return True
    for record in db.scalars(
        select(Resource).where(
            Resource.resource_type.in_(
                {
                    "post",
                    "approval",
                    "doc",
                    "notice",
                    "event",
                    "inquiry",
                    "finance_contract",
                }
            )
        )
    ):
        data = record.payload or {}
        refs = _upload_reference_ids(
            [
                data.get("files", []),
                data.get("attachments", []),
                data.get("fileId"),
                data.get("contractFile"),
            ]
        )
        if str(upload.id) not in refs:
            continue
        if record.resource_type == "post":
            if data.get("status", "published") == "published":
                return True
        elif record.resource_type == "approval":
            if record.owner_id == user.id or any(
                str(step.get("userId")) == str(user.id) for step in data.get("line", [])
            ):
                return True
        elif record.resource_type == "doc":
            if (
                str(data.get("type", "")).upper() != "FINANCE"
                and str(data.get("category", "")).upper() != "FINANCE"
            ) or user_can_finance(user):
                return True
        elif record.resource_type in {"notice", "event"}:
            return True
        elif record.resource_type == "inquiry":
            if (
                is_record_owner(record, user)
                or "Admin" in (user.roles or [])
                or user.department == data.get("dept")
            ):
                return True
        elif record.resource_type == "finance_contract" and user_can_finance(user):
            return True
    return False


def require_admin(user: User) -> None:
    if "Admin" not in (user.roles or []):
        raise HTTPException(403, "관리자 권한이 필요합니다.")


def require_inquiry_access(user: User, inquiry: Resource) -> None:
    if "Admin" not in (user.roles or []) and (
        not user.department or user.department != inquiry.payload.get("dept")
    ):
        raise HTTPException(403, "해당 부서 문의를 처리할 권한이 없습니다.")


def _announce_all_users(kind: str, title: str, href: str) -> None:
    with SessionLocal() as db:
        user_ids = list(db.scalars(select(User.id)))
        for user_id in user_ids:
            try:
                notify_user(db, user_id, kind, title, href)
            except Exception:
                logger.exception(
                    "Could not notify user %s about an announcement", user_id
                )


def _announce(
    background_tasks: BackgroundTasks, kind: str, title: str, href: str
) -> None:
    background_tasks.add_task(_announce_all_users, kind, title, href)


@router.post("/admin/projects", status_code=201, summary="프로젝트 등록")
def create_project(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    if not payload.get("name"):
        raise HTTPException(400, "프로젝트 이름은 필수입니다.")
    slug = payload.get("slug")
    if slug and db.scalar(
        select(Resource.id).where(
            Resource.resource_type == "project",
            Resource.payload["slug"].as_string() == slug,
        )
    ):
        raise HTTPException(409, "이미 사용 중인 프로젝트 slug입니다.")
    data = {
        "memberIds": [],
        "members": [],
        "openings": [],
        "progress": 0,
        "status": "진행 중",
        **payload,
    }
    record = create_record(db, "project", data, user)
    return envelope(
        {"id": str(record.id), "slug": data.get("slug", str(record.id))},
        "프로젝트를 등록했습니다.",
        201,
    )


@router.patch("/admin/projects/{project_id}", summary="프로젝트 수정")
def patch_project(
    project_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    project = None
    try:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project", Resource.id == int(project_id)
            )
        )
    except ValueError:
        pass
    if project is None:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project",
                Resource.payload["slug"].as_string() == project_id,
            )
        )
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    updated = update_record(db, project, {**project.payload, **payload})
    return envelope(
        {"id": str(updated.id), **updated.payload}, "프로젝트를 수정했습니다."
    )


@router.post("/uploads", status_code=201, summary="10MB 이하 파일 업로드")
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "파일은 10MB 이하만 업로드할 수 있습니다.")
    content_type = file.content_type or "application/octet-stream"
    if content_type == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(415, "JPG 파일 형식이 올바르지 않습니다.")
    if content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(415, "PNG 파일 형식이 올바르지 않습니다.")
    suffix = Path(Path(file.filename or "file").name).suffix[:16]
    file_id = uuid.uuid4().hex
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{file_id}{suffix}"
    path.write_bytes(data)
    record = create_record(
        db,
        "upload",
        {
            "filename": Path(file.filename or "file").name,
            "path": str(path),
            "contentType": content_type,
            "size": len(data),
        },
        user,
    )
    return envelope(
        {
            "id": str(record.id),
            "url": f"/uploads/{record.id}",
            "filename": record.payload["filename"],
            "size": len(data),
        },
        "파일을 업로드했습니다.",
        201,
    )


@router.get("/uploads/{upload_id}", summary="업로드 파일 다운로드")
def download_file(
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "upload", upload_id)
    if not _can_download_upload(db, record, user):
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    path = Path(record.payload["path"])
    if not path.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(
        path,
        filename=record.payload.get("filename", path.name),
        media_type=record.payload.get("contentType"),
    )


@router.post("/admin/notices", status_code=201, summary="공지 작성")
def create_notice(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    for field in ("kind", "title", "body"):
        if not payload.get(field):
            raise HTTPException(400, f"{field} 항목은 필수입니다.")
    data = {
        **payload,
        "createdAt": iso(datetime.now().astimezone()),
        "author": user.name,
        "pinned": bool(payload.get("pinned", False)),
        "mustRead": bool(payload.get("mustRead", False)),
        "confirmed": 0,
        "total": db.query(User).count(),
    }
    record = create_record(db, "notice", data, user)
    _announce(
        background_tasks,
        "공지",
        f"새 공지: {payload['title']}",
        f"/inhouse/community/notices/{record.id}",
    )
    return envelope({"id": str(record.id)}, "공지를 등록했습니다.", 201)


@router.patch("/admin/notices/{notice_id}", summary="공지 수정 및 고정")
def patch_notice(
    notice_id: str,
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    notice = get_record(db, "notice", notice_id)
    updated = update_record(
        db,
        notice,
        {**notice.payload, **payload, "updatedAt": iso(datetime.now().astimezone())},
    )
    if any(key in payload for key in ("title", "body", "pinned", "mustRead")):
        _announce(
            background_tasks,
            "공지",
            f"공지 업데이트: {updated.payload.get('title', '')}",
            f"/inhouse/community/notices/{notice_id}",
        )
    return envelope({"id": notice_id, **updated.payload}, "공지를 수정했습니다.")


@router.get("/admin/notices/{notice_id}/unconfirmed", summary="공지 미확인자")
def unconfirmed_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    get_record(db, "notice", notice_id)
    read_ids = {
        r.owner_id
        for r in db.scalars(
            select(Resource).where(
                Resource.resource_type == "notice_confirmation",
                Resource.payload["noticeId"].as_string() == notice_id,
            )
        )
    }
    people = [
        {"id": str(p.id), "name": p.name, "email": p.email}
        for p in db.scalars(select(User))
        if p.id not in read_ids
    ]
    return envelope({"items": people, "nextCursor": None})


@router.post("/admin/events", status_code=201, summary="일정 등록")
def create_event(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    if not payload.get("date") or not payload.get("title"):
        raise HTTPException(400, "일정 날짜와 제목을 입력해주세요.")
    record = create_record(db, "event", {**payload, "rsvpByUser": {}}, user)
    _announce(
        background_tasks, "일정", f"새 일정: {payload['title']}", "/inhouse/my/calendar"
    )
    return envelope({"id": str(record.id)}, "일정을 등록했습니다.", 201)


@router.patch("/admin/events/{event_id}", summary="일정 수정")
def patch_event(
    event_id: str,
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    event = get_record(db, "event", event_id)
    previous = dict(event.payload)
    updated = update_record(db, event, {**event.payload, **payload})
    if any(previous.get(key) != updated.payload.get(key) for key in payload):
        _announce(
            background_tasks,
            "일정",
            f"일정이 변경됐어요: {updated.payload.get('title', previous.get('title', ''))}",
            "/inhouse/my/calendar",
        )
    return envelope({"id": event_id, **updated.payload}, "일정을 수정했습니다.")


@router.post("/admin/docs", status_code=201, summary="문서 등록 또는 개정")
def create_or_revise_doc(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    slug = payload.get("slug")
    if not slug or not payload.get("title"):
        raise HTTPException(400, "문서 slug와 제목은 필수입니다.")
    existing = db.scalar(
        select(Resource).where(
            Resource.resource_type == "doc",
            Resource.payload["slug"].as_string() == slug,
        )
    )
    if existing is None:
        doc = create_record(
            db,
            "doc",
            {
                **payload,
                "owner": user.name,
                "date": payload.get("date", iso(datetime.now().astimezone())),
            },
            user,
        )
        create_record(
            db,
            "doc_revision",
            {
                "slug": slug,
                "version": payload.get("version", "v1.0"),
                "ver": payload.get("version", "v1.0"),
                "date": doc.payload.get("date"),
                "who": user.name,
                "note": payload.get("note", "최초 등록"),
                "document": doc.payload,
            },
            user,
        )
        return envelope({"slug": slug, "id": str(doc.id)}, "문서를 등록했습니다.", 201)
    old_version = existing.payload.get("version", "v1.0")
    old_doc = dict(existing.payload)
    version = payload.get("version") or _next_version(old_version)
    revision = create_record(
        db,
        "doc_revision",
        {
            "slug": slug,
            "version": old_version,
            "ver": old_version,
            "date": old_doc.get("date", iso(datetime.now().astimezone())),
            "who": user.name,
            "note": payload.get("note", "문서 개정"),
            "document": old_doc,
        },
        user,
    )
    revised = {
        **existing.payload,
        **payload,
        "version": version,
        "date": iso(datetime.now().astimezone()),
        "effective": payload.get("effective"),
        "owner": user.name,
        "revised": True,
    }
    update_record(db, existing, revised)
    if payload.get("notify", True):
        _announce(
            background_tasks,
            "공지",
            f"문서가 개정됐어요: {revised['title']}",
            f"/inhouse/documents/{slug}",
        )
    return envelope(
        {"slug": slug, "version": version, "revisionId": str(revision.id)},
        "문서를 개정했습니다.",
    )


def _next_version(version: str) -> str:
    import re

    match = re.fullmatch(r"v(\d+)\.(\d+)", str(version))
    if not match:
        return "v1.1"
    return f"v{match.group(1)}.{int(match.group(2)) + 1}"


@router.get("/admin/inquiries", summary="관리자 문의 목록")
def admin_inquiries(
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    is_admin = "Admin" in (user.roles or [])
    if not is_admin and not user.department:
        raise HTTPException(403, "관리자 또는 담당 부서 권한이 필요합니다.")
    stmt = select(Resource).where(Resource.resource_type == "inquiry")
    if not is_admin:
        stmt = stmt.where(Resource.payload["dept"].as_string() == user.department)
    if status:
        stmt = stmt.where(Resource.payload["status"].as_string() == status)
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.order_by(Resource.id.desc()).limit(limit + 1)))
    page = records[:limit]
    return envelope(
        {
            "items": [
                {"id": str(record.id), **public_content_payload(record.payload)}
                for record in page
            ],
            "nextCursor": str(page[-1].id) if len(records) > limit and page else None,
        }
    )


@router.post("/admin/inquiries/{inquiry_id}/answer", summary="문의 답변 작성")
def answer_inquiry(
    inquiry_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inquiry = get_record(db, "inquiry", inquiry_id)
    require_inquiry_access(user, inquiry)
    text = payload.get("text", "").strip()
    if not text:
        raise HTTPException(400, "답변을 입력해주세요.")
    answer = {
        "who": user.name,
        "whoId": str(user.id),
        "at": iso(datetime.now().astimezone()),
        "text": text,
    }
    data = {
        **inquiry.payload,
        "status": "답변 완료",
        "answer": answer,
        "steps": [*inquiry.payload.get("steps", [])[:2], answer["at"]],
    }
    update_record(db, inquiry, data)
    recipient = (
        db.get(User, inquiry.owner_id)
        if inquiry.owner_id
        else resolve_anonymous_owner(db, inquiry.payload.get("anonymousOwnerKey"))
    )
    notify_user(
        db,
        recipient.id if recipient else None,
        "내 업무",
        "문의에 답변이 등록됐어요.",
        f"/inhouse/community/inquiry?id={inquiry.id}",
    )
    return envelope(
        {"id": inquiry_id, "status": "답변 완료", "answer": answer},
        "답변을 등록했습니다.",
    )


@router.patch("/admin/inquiries/{inquiry_id}", summary="문의 상태 변경")
def patch_inquiry(
    inquiry_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inquiry = get_record(db, "inquiry", inquiry_id)
    require_inquiry_access(user, inquiry)
    state = payload.get("status")
    if state not in {"접수", "검토 중", "답변 완료", "해결됨"}:
        raise HTTPException(422, "문의 상태가 올바르지 않습니다.")
    data = {**inquiry.payload, "status": state}
    update_record(db, inquiry, data)
    recipient = (
        db.get(User, inquiry.owner_id)
        if inquiry.owner_id
        else resolve_anonymous_owner(db, inquiry.payload.get("anonymousOwnerKey"))
    )
    if recipient:
        notify_user(
            db,
            recipient.id,
            "내 업무",
            f"문의 처리 상태가 변경됐어요: {state}",
            f"/inhouse/community/inquiry?id={inquiry_id}",
        )
    return envelope({"id": inquiry_id, "status": state}, "문의 상태를 변경했습니다.")


@router.get("/admin/reports", summary="신고 목록")
def get_reports(
    status: str | None = "PENDING",
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    stmt = select(Resource).where(Resource.resource_type == "report")
    if status:
        stmt = stmt.where(Resource.payload["status"].as_string() == status)
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.order_by(Resource.id.desc()).limit(limit + 1)))
    page = records[:limit]
    return envelope(
        {
            "items": [{"id": str(record.id), **record.payload} for record in page],
            "nextCursor": str(page[-1].id) if len(records) > limit and page else None,
        }
    )


@router.post("/admin/reports/{report_id}/decision", summary="신고 처리")
def decide_report(
    report_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    report = get_record(db, "report", report_id)
    decision = payload.get("decision")
    if decision not in {"숨김", "삭제", "반려"}:
        raise HTTPException(422, "처리 결과는 숨김, 삭제, 반려 중 하나여야 합니다.")
    post = get_record(db, "post", report.payload["postId"])
    if decision in {"숨김", "삭제"}:
        post.payload = {
            **post.payload,
            "status": "hidden" if decision == "숨김" else "deleted",
        }
    update_record(
        db,
        report,
        {
            **report.payload,
            "status": decision,
            "handledBy": user.name,
            "handledAt": iso(datetime.now().astimezone()),
            "note": payload.get("note", ""),
        },
    )
    db.commit()
    return envelope({"id": report_id, "status": decision}, "신고를 처리했습니다.")


@router.get("/admin/proposals", summary="관리자 운영 제안 목록")
def admin_proposals(
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if "Admin" not in (user.roles or []) and not user.department:
        raise HTTPException(403, "관리자 또는 담당 부서 권한이 필요합니다.")
    stmt = select(Resource).where(Resource.resource_type == "proposal")
    if "Admin" not in (user.roles or []):
        stmt = stmt.where(Resource.payload["dept"].as_string() == user.department)
    if status:
        status_expr = func.coalesce(Resource.payload["status"].as_string(), "접수")
        stmt = stmt.where(status_expr == status)
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.order_by(Resource.id.desc()).limit(limit + 1)))
    page = records[:limit]
    return envelope(
        {
            "items": [
                {"id": str(record.id), **public_content_payload(record.payload)}
                for record in page
            ],
            "nextCursor": str(page[-1].id) if len(records) > limit and page else None,
        }
    )


@router.patch(
    "/admin/proposals/{proposal_id}", summary="운영 제안 검토 상태 및 답변 수정"
)
def patch_proposal(
    proposal_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = get_record(db, "proposal", proposal_id)
    if "Admin" not in (user.roles or []) and (
        not user.department or user.department != proposal.payload.get("dept")
    ):
        raise HTTPException(403, "제안을 처리할 담당 부서 권한이 없습니다.")
    allowed_states = {"접수", "검토 중", "협업 중", "답변 완료", "반려"}
    status = payload.get("status", proposal.payload.get("status", "접수"))
    if status not in allowed_states:
        raise HTTPException(422, "운영 제안 상태가 올바르지 않습니다.")
    data = {**proposal.payload, "status": status}
    history = list(data.get("history", []))
    if status != proposal.payload.get("status"):
        history.append(
            {
                "status": status,
                "who": user.name,
                "at": iso(datetime.now().astimezone()),
                "note": payload.get("note", ""),
            }
        )
    data["history"] = history
    answer_text = payload.get("answer", payload.get("text"))
    if answer_text is not None:
        if not str(answer_text).strip():
            raise HTTPException(422, "공식 답변을 입력해주세요.")
        data["answer"] = {
            "who": user.name,
            "at": iso(datetime.now().astimezone()),
            "text": str(answer_text).strip(),
        }
        if status not in {"반려"}:
            data["status"] = "답변 완료"
            history.append(
                {
                    "status": "답변 완료",
                    "who": user.name,
                    "at": data["answer"]["at"],
                }
            )
    update_record(db, proposal, data)
    recipient = (
        db.get(User, proposal.owner_id)
        if proposal.owner_id
        else resolve_anonymous_owner(db, proposal.payload.get("anonymousOwnerKey"))
    )
    if recipient:
        notify_user(
            db,
            recipient.id,
            "공지",
            f"운영 제안 상태가 변경됐어요: {data.get('title', '')}",
            f"/inhouse/community/proposals/{proposal_id}",
        )
    return envelope(
        {"id": proposal_id, "status": data["status"], "answer": data.get("answer")},
        "운영 제안을 갱신했습니다.",
    )


@router.get("/admin/finance/permissions", summary="재정 권한 조회")
def finance_permissions(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    require_admin(user)
    return envelope(
        {
            "items": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "email": p.email,
                    "roles": p.roles or [],
                    "canViewFinance": bool(
                        set(p.roles or []).intersection({"Admin", "FA", "Leader"})
                    ),
                }
                for p in db.scalars(select(User))
            ],
            "nextCursor": None,
        }
    )


@router.patch(
    "/admin/finance/permissions/{user_id}", summary="재정 열람 권한 부여·회수"
)
def change_finance_permission(
    user_id: int,
    payload: dict[str, bool],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "사용자를 찾을 수 없습니다.")
    roles = set(target.roles or [])
    if payload.get("canViewFinance"):
        if not roles.intersection({"FA", "Leader", "Admin"}):
            roles.add("FA")
    else:
        roles.discard("FA")
    target.roles = sorted(roles)
    db.commit()
    return envelope(
        {
            "id": str(target.id),
            "roles": target.roles,
            "canViewFinance": bool(roles.intersection({"FA", "Leader", "Admin"})),
        },
        "재정 권한을 갱신했습니다.",
    )


@router.get("/admin/signup-requests", summary="가입 요청 목록")
def signup_requests(
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    records, next_cursor = list_records(db, "signup_request", cursor, limit)
    items = []
    for record in records:
        data = dict(record.payload)
        data.pop("passwordHash", None)
        items.append({"id": str(record.id), **data})
    return envelope(
        {
            "items": items,
            "nextCursor": next_cursor,
        }
    )


@router.post("/admin/signup-requests/{request_id}/approve", summary="가입 요청 승인")
def approve_signup(
    request_id: str,
    payload: dict[str, str],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_admin(user)
    request = get_record(db, "signup_request", request_id)
    email = request.payload.get("email", "")
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "이메일이 이미 등록되었습니다.")
    password_hash = request.payload.get("passwordHash")
    temporary_password = payload.get("temporaryPassword", "")
    if temporary_password:
        if len(temporary_password) < 8:
            raise HTTPException(400, "초기 비밀번호는 8자 이상이어야 합니다.")
        from app.services.security import hash_password

        password_hash = hash_password(temporary_password)
    if not password_hash:
        raise HTTPException(
            400, "가입 요청에 비밀번호가 없으므로 초기 비밀번호가 필요합니다."
        )

    member = User(
        email=email,
        name=request.payload["name"],
        department=request.payload.get("dept"),
        password_hash=password_hash,
        roles=["Member"],
    )
    db.add(member)
    db.flush()
    request.payload = {
        **request.payload,
        "status": "APPROVED",
        "approvedBy": user.name,
        "userId": member.id,
    }
    db.commit()
    notify_user(
        db,
        member.id,
        "내 업무",
        "인하우스 계정 요청이 승인되었습니다.",
        "/inhouse/login",
    )
    background_tasks.add_task(_send_signup_approved_email, member.email, member.name)
    return envelope(
        {"userId": str(member.id), "status": "APPROVED"}, "가입 요청을 승인했습니다."
    )
