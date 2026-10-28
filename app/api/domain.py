from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User

router = APIRouter(
    tags=[
        "workspace",
        "projects",
        "documents",
        "community",
        "rules",
        "finance",
        "search",
    ]
)


def ok(data: Any, message: str = "조회 성공", code: int = 200) -> dict:
    return {"code": code, "message": message, "data": data}


def save(db: Session, kind: str, payload: dict, owner: User | None = None) -> Resource:
    record = Resource(
        resource_type=kind, payload=payload, owner_id=owner.id if owner else None
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def find(db: Session, kind: str, record_id: int) -> Resource:
    record = db.scalar(
        select(Resource).where(Resource.id == record_id, Resource.resource_type == kind)
    )
    if record is None:
        raise HTTPException(404, "요청한 정보를 찾을 수 없습니다.")
    return record


def listing(
    db: Session, kind: str, page: int = 1, limit: int = 20
) -> tuple[list[Resource], int]:
    stmt = (
        select(Resource)
        .where(Resource.resource_type == kind)
        .order_by(Resource.created_at.desc())
    )
    records = list(db.scalars(stmt.offset((page - 1) * limit).limit(limit)))
    total = (
        db.scalar(select(Resource.id).where(Resource.resource_type == kind).count())
        if False
        else None
    )
    from sqlalchemy import func

    total = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(Resource.resource_type == kind)
        )
        or 0
    )
    return records, total


def item(record: Resource) -> dict:
    return {"id": record.id, **record.payload}


def require_roles(user: User, *roles: str) -> None:
    if not set(user.roles or []).intersection(roles):
        raise HTTPException(403, "요청을 수행할 권한이 없습니다.")


@router.get("/users", summary="팀원 조회")
def list_users(
    department: str | None = None,
    role: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(User)
    if department:
        stmt = stmt.where(User.department == department)
    users = list(
        db.scalars(stmt.order_by(User.id).offset((page - 1) * limit).limit(limit))
    )
    from sqlalchemy import func

    total = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.department == department if department else True)
        )
        or 0
    )
    values = [
        {
            "id": u.id,
            "name": u.name,
            "department": u.department,
            "role": (u.roles or [""])[0],
        }
        for u in users
        if not role or role in (u.roles or [])
    ]
    return ok({"list": values, "totalCount": total, "page": page, "limit": limit})


@router.get("/users/{user_id}", summary="팀원 상세 조회")
def get_user(
    user_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "해당 구성원을 찾을 수 없습니다.")
    projects, _ = listing(db, "project", 1, 100)
    return ok(
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "projects": [
                item(p) for p in projects if user.id in p.payload.get("memberIds", [])
            ],
            "skills": user.__dict__.get("skills", []),
        }
    )


@router.get("/organization/tree", summary="조직도 조회")
def organization_tree(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    departments: dict[str, list] = {}
    for user in db.scalars(select(User).order_by(User.name)):
        name = user.department or "미지정"
        departments.setdefault(name, []).append(
            {"id": user.id, "name": user.name, "role": (user.roles or [""])[0]}
        )
    return ok(
        {
            "departments": [
                {"id": idx, "name": name, "members": members}
                for idx, (name, members) in enumerate(departments.items(), 1)
            ]
        }
    )


@router.get("/dashboard/home", summary="대시보드 조회")
def dashboard(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    todos, _ = listing(db, "todo", 1, 100)
    notices, _ = listing(db, "post", 1, 100)
    projects, _ = listing(db, "project", 1, 100)
    notice = next((item(p) for p in notices if p.payload.get("isNotice")), None)
    data = {
        "summary": {
            "todoCount": len([t for t in todos if t.owner_id == user.id]),
            "notice": notice,
            "schedule": [],
        }
    }
    if set(user.roles or []).intersection({"Admin", "FA", "Leader", "PM"}):
        data["finance"] = {"currentBalance": 0}
    return ok(data)


@router.get("/workspace/todos", summary="todo 조회")
def get_todos(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    records = db.scalars(
        select(Resource)
        .where(Resource.resource_type == "todo", Resource.owner_id == user.id)
        .order_by(Resource.created_at.desc())
    )
    return ok({"list": [item(r) for r in records]})


@router.get("/notifications", summary="알림 조회")
def get_notifications(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    records = db.scalars(
        select(Resource)
        .where(Resource.resource_type == "notification", Resource.owner_id == user.id)
        .order_by(Resource.created_at.desc())
    )
    return ok({"list": [item(r) for r in records]})


@router.patch("/notifications/{notification_id}/read", summary="알림 읽음 처리")
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = db.scalar(
        select(Resource).where(
            Resource.id == notification_id,
            Resource.resource_type == "notification",
            Resource.owner_id == user.id,
        )
    )
    if record is None:
        raise HTTPException(404, "알림을 찾을 수 없습니다.")
    record.payload = {**record.payload, "isRead": True}
    db.commit()
    return ok({"notificationId": record.id, "isRead": True}, "읽음 처리 완료")


@router.post("/notifications/push-token", summary="FCM 토큰 등록")
def register_push_token(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if not payload.get("token") or not payload.get("deviceType"):
        raise HTTPException(400, "토큰 정보가 누락되었습니다.")
    existing = db.scalar(
        select(Resource).where(
            Resource.resource_type == "push_token", Resource.owner_id == user.id
        )
    )
    if existing:
        existing.payload = payload
        db.commit()
    else:
        save(db, "push_token", payload, user)
    return ok({"success": True}, "토큰 등록 완료")


@router.get("/projects", summary="프로젝트 목록 조회")
def get_projects(
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    records, total = listing(db, "project", page, limit)
    values = [
        item(p)
        for p in records
        if not status_filter or p.payload.get("status") == status_filter
    ]
    return ok({"list": values, "totalCount": total, "page": page, "limit": limit})


@router.get("/projects/{project_id}", summary="프로젝트 상세 및 참여자 조회")
def get_project(
    project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    project = find(db, "project", project_id)
    members = [
        db.get(User, member_id) for member_id in project.payload.get("memberIds", [])
    ]
    return ok(
        {
            **item(project),
            "members": [
                {"id": m.id, "name": m.name, "role": (m.roles or [""])[0]}
                for m in members
                if m
            ],
        }
    )


def create_recruitment(project_id: int, payload: dict, db: Session, user: User):
    find(db, "project", project_id)
    require_roles(user, "Admin", "PM")
    for key in ("major", "count", "dueDate", "requirements"):
        if key not in payload:
            raise HTTPException(400, f"{key} 항목은 필수입니다.")
    record = save(
        db, "recruitment", {**payload, "projectId": project_id, "status": "OPEN"}, user
    )
    return ok(
        {"recruitmentId": record.id, "status": "OPEN"},
        "모집 공고가 등록되었습니다.",
        201,
    )


@router.post("/projects/{project_id}/recruitments", summary="구인 공고 등록")
def register_recruitment(
    project_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return create_recruitment(project_id, payload, db, user)


@router.post("/projects/{project_id}/apply", summary="구직 신청")
def apply_project(
    project_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    find(db, "project", project_id)
    if db.scalar(
        select(Resource).where(
            Resource.resource_type == "application",
            Resource.owner_id == user.id,
            Resource.payload["projectId"].as_integer() == project_id,
        )
    ):
        raise HTTPException(409, "이미 참여 신청한 프로젝트입니다.")
    if not payload.get("reason"):
        raise HTTPException(400, "지원 동기를 입력해주세요.")
    record = save(
        db,
        "application",
        {**payload, "projectId": project_id, "status": "APPLIED"},
        user,
    )
    return ok(
        {"applicationId": record.id, "status": "APPLIED"}, "신청이 완료되었습니다.", 201
    )


@router.post("/documents", summary="문서 작성 및 임시저장")
def create_document(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    for key in ("type", "title", "content", "isDraft"):
        if key not in payload:
            raise HTTPException(400, f"필수 입력 항목이 누락되었습니다: {key}")
    record = save(
        db,
        "document",
        {
            **payload,
            "status": "DRAFT" if payload["isDraft"] else "DRAFT",
            "history": [],
        },
        user,
    )
    return ok(
        {"documentId": record.id, "status": record.payload["status"]},
        "문서가 성공적으로 저장되었습니다.",
        201,
    )


@router.get("/documents/{document_id}", summary="문서 상세 및 결재 이력 조회")
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = find(db, "document", document_id)
    if record.owner_id != user.id and record.payload.get("approverId") != user.id:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return ok(item(record))


@router.post("/documents/{document_id}/submit", summary="문서 제출")
def submit_document(
    document_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = find(db, "document", document_id)
    if record.owner_id != user.id:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if record.payload.get("status") != "DRAFT":
        raise HTTPException(400, "이미 결재가 진행 중이거나 완료된 문서입니다.")
    approver_id = payload.get("approverId")
    if not approver_id or db.get(User, approver_id) is None:
        raise HTTPException(400, "결재 담당자를 확인해주세요.")
    approval = save(
        db,
        "approval",
        {"documentId": record.id, "approverId": approver_id, "status": "PENDING"},
        user,
    )
    record.payload = {
        **record.payload,
        "status": "PENDING",
        "approverId": approver_id,
        "approvalId": approval.id,
        "history": [{"step": 1, "status": "SUBMITTED", "actor": user.name}],
    }
    db.commit()
    return ok(
        {"documentId": record.id, "status": "PENDING"}, "결재 요청이 완료되었습니다."
    )


@router.post("/approvals/{approval_id}/decide", summary="문서 결재")
def decide_approval(
    approval_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    approval = find(db, "approval", approval_id)
    if approval.payload.get("approverId") != user.id:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    action = payload.get("action")
    if action not in {"APPROVE", "REJECT"}:
        raise HTTPException(400, "action은 APPROVE 또는 REJECT여야 합니다.")
    if action == "REJECT" and not payload.get("rejectReason"):
        raise HTTPException(400, "문서 반려 시에는 반려 사유가 필수입니다.")
    result = "APPROVED" if action == "APPROVE" else "REJECTED"
    approval.payload = {
        **approval.payload,
        "status": result,
        "rejectReason": payload.get("rejectReason"),
    }
    doc = find(db, "document", approval.payload["documentId"])
    doc.payload = {
        **doc.payload,
        "status": result,
        "history": [
            *doc.payload.get("history", []),
            {
                "step": 2,
                "status": result,
                "actor": user.name,
                "rejectReason": payload.get("rejectReason"),
            },
        ],
    }
    db.commit()
    return ok(
        {"approvalId": approval.id, "result": result}, "결재 처리가 완료되었습니다."
    )


@router.get("/posts", summary="게시글 조회")
def get_posts(
    category: str | None = None,
    sort: str = "LATEST",
    page: int = Query(1, ge=1),
    limit: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    records, total = listing(db, "post", page, limit)
    values = []
    for post in records:
        if category and post.payload.get("category") != category:
            continue
        author = db.get(User, post.owner_id) if post.owner_id else None
        values.append(
            {
                "id": post.id,
                "title": post.payload.get("title"),
                "authorName": "익명"
                if post.payload.get("isAnonymous")
                else (author.name if author else "알 수 없음"),
                "likeCount": post.payload.get("likeCount", 0),
                "createdAt": post.created_at.isoformat() if post.created_at else None,
                **{
                    k: v
                    for k, v in post.payload.items()
                    if k in {"content", "category", "isNotice"}
                },
            }
        )
    if sort == "POPULAR":
        values.sort(key=lambda x: x["likeCount"], reverse=True)
    return ok({"list": values, "totalCount": total, "page": page, "limit": limit})


@router.post("/posts", summary="게시글 등록", status_code=201)
def create_post(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if (
        not all(payload.get(k) for k in ("title", "content", "category"))
        or "isAnonymous" not in payload
    ):
        raise HTTPException(400, "제목, 내용, 카테고리, 익명 여부는 필수입니다.")
    if payload.get("isNotice") and "Admin" not in (user.roles or []):
        raise HTTPException(403, "공지사항 등록 권한이 없습니다.")
    post = save(
        db,
        "post",
        {**payload, "isNotice": bool(payload.get("isNotice", False)), "likeCount": 0},
        user,
    )
    return ok({"postId": post.id}, "게시글이 등록되었습니다.", 201)


@router.post(
    "/posts/{post_id}/comments", summary="댓글 및 대댓글 등록", status_code=201
)
def create_comment(
    post_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    find(db, "post", post_id)
    if not payload.get("content") or "isAnonymous" not in payload:
        raise HTTPException(400, "댓글 내용과 익명 여부는 필수입니다.")
    parent_id = payload.get("parentId")
    if parent_id:
        parent = find(db, "comment", parent_id)
        if parent.payload.get("postId") != post_id:
            raise HTTPException(404, "상위 댓글이 존재하지 않습니다.")
    record = save(db, "comment", {**payload, "postId": post_id}, user)
    return ok({"commentId": record.id}, "댓글이 등록되었습니다.", 201)


@router.post("/posts/{post_id}/like", summary="게시글 추천 토글")
def toggle_post_like(
    post_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = find(db, "post", post_id)
    reaction = db.scalar(
        select(Resource).where(
            Resource.resource_type == "post_like",
            Resource.owner_id == user.id,
            Resource.payload["postId"].as_integer() == post_id,
        )
    )
    if reaction:
        db.delete(reaction)
        liked = False
    else:
        save(db, "post_like", {"postId": post_id}, user)
        liked = True
    from sqlalchemy import func

    count = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "post_like",
                Resource.payload["postId"].as_integer() == post_id,
            )
        )
        or 0
    )
    post.payload = {**post.payload, "likeCount": count}
    db.commit()
    return ok({"liked": liked, "likeCount": count}, "추천 상태가 변경되었습니다.")


@router.post("/proposals", summary="운영 개선 제안 등록", status_code=201)
def create_proposal(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if not payload.get("title") or not payload.get("content"):
        raise HTTPException(400, "제목과 내용을 입력해주세요.")
    record = save(db, "proposal", {**payload, "recommendCount": 0}, user)
    return ok({"proposalId": record.id}, "제안이 등록되었습니다.", 201)


@router.post("/proposals/{proposal_id}/recommend", summary="운영 제안 추천 토글")
def toggle_recommend(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = find(db, "proposal", proposal_id)
    reaction = db.scalar(
        select(Resource).where(
            Resource.resource_type == "proposal_recommend",
            Resource.owner_id == user.id,
            Resource.payload["proposalId"].as_integer() == proposal_id,
        )
    )
    if reaction:
        db.delete(reaction)
        recommended = False
    else:
        save(db, "proposal_recommend", {"proposalId": proposal_id}, user)
        recommended = True
    from sqlalchemy import func

    count = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "proposal_recommend",
                Resource.payload["proposalId"].as_integer() == proposal_id,
            )
        )
        or 0
    )
    proposal.payload = {**proposal.payload, "recommendCount": count}
    db.commit()
    return ok(
        {"recommended": recommended, "recommendCount": count},
        "추천 상태가 변경되었습니다.",
    )


@router.post("/inquiries", summary="익명 문의 작성", status_code=201)
def create_inquiry(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if not all(payload.get(k) for k in ("title", "content", "targetDept")):
        raise HTTPException(400, "담당 조직과 문의 내용을 입력해주세요.")
    record = save(
        db, "inquiry", {**payload, "status": "RECEIVED", "answerContent": None}, user
    )
    return ok(
        {"inquiryId": record.id, "status": "RECEIVED"},
        "문의가 성공적으로 접수되었습니다.",
        201,
    )


@router.post("/inquiries/{inquiry_id}/answer", summary="문의 공식 답변 작성")
def answer_inquiry(
    inquiry_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    inquiry = find(db, "inquiry", inquiry_id)
    if not set(user.roles or []).intersection(
        {"Admin", inquiry.payload.get("targetDept")}
    ):
        raise HTTPException(403, "답변 작성 권한이 없습니다.")
    if not payload.get("answerContent"):
        raise HTTPException(400, "답변 내용을 입력해주세요.")
    inquiry.payload = {
        **inquiry.payload,
        "status": "ANSWERED",
        "answerContent": payload["answerContent"],
    }
    db.commit()
    save(
        db,
        "notification",
        {
            "type": "INQUIRY_ANSWERED",
            "title": "문의 답변이 등록되었습니다.",
            "inquiryId": inquiry.id,
            "isRead": False,
        },
        db.get(User, inquiry.owner_id) if inquiry.owner_id else None,
    )
    return ok({"inquiryId": inquiry.id, "status": "ANSWERED"}, "답변이 등록되었습니다.")


@router.get("/rules", summary="규정 목록 및 분류 조회")
def get_rules(
    category: str | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    records, total = listing(db, "rule", 1, 1000)
    values = [
        item(r)
        for r in records
        if (not category or r.payload.get("category") == category)
        and (
            not keyword
            or keyword.lower()
            in (r.payload.get("title", "") + r.payload.get("content", "")).lower()
        )
    ]
    return ok({"list": values, "totalCount": len(values)})


@router.get("/rules/{rule_id}", summary="규정 상세 및 개정 이력 조회")
def get_rule(
    rule_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    return ok(item(find(db, "rule", rule_id)))


@router.get("/finance/dashboard", summary="재정 전체 조회")
def finance_dashboard(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    require_roles(user, "Admin", "FA", "Leader", "PM")
    orders, _ = listing(db, "finance_order", 1, 1000)
    deposits, _ = listing(db, "finance_deposit", 1, 1000)
    order_total = sum(x.payload.get("amount", 0) for x in orders)
    deposit_total = sum(x.payload.get("amount", 0) for x in deposits)
    return ok(
        {
            "currentBalance": deposit_total,
            "totalOrderAmount": order_total,
            "totalDepositAmount": deposit_total,
            "recentOrders": [item(x) for x in orders[:10]],
            "recentDeposits": [item(x) for x in deposits[:10]],
        }
    )


@router.get("/finance/projects", summary="프로젝트별 재정 현황 조회")
def finance_projects(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_roles(user, "FA", "Leader", "PM", "Admin")
    records, total = listing(db, "finance_project", page, limit)
    return ok(
        {
            "list": [item(r) for r in records],
            "totalCount": total,
            "page": page,
            "limit": limit,
        }
    )


@router.get("/search", summary="통합 검색 조회")
def search(
    query: str = Query(..., min_length=2),
    type: str = "ALL",
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    kinds = {
        "PROJECT": "project",
        "POST": "post",
        "RULE": "rule",
        "PROPOSAL": "proposal",
    }
    selected = (
        list(kinds.items())
        if type == "ALL"
        else [(type, kinds[type])]
        if type in kinds
        else []
    )
    results = []
    for category, kind in selected:
        records = db.scalars(
            select(Resource)
            .where(Resource.resource_type == kind)
            .order_by(Resource.created_at.desc())
            .limit(100)
        )
        for record in records:
            if kind == "post" and record.payload.get("isAnonymous"):
                continue
            title = str(record.payload.get("title", record.payload.get("name", "")))
            content = str(
                record.payload.get("content", record.payload.get("description", ""))
            )
            if query.lower() in (title + " " + content).lower():
                results.append(
                    {
                        "category": category,
                        "id": str(record.id),
                        "title": title,
                        "snippet": content[:160],
                    }
                )
    return ok({"result": results}, "검색 성공")
