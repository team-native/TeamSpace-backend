import secrets
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Numeric, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    get_record,
    iso,
    notify_user,
    require_finance,
    update_record,
)
from app.db.session import get_db
from app.models.bank import BankSyncRun
from app.models.resource import Resource
from app.models.user import User
from app.services.anonymity import is_record_owner
from app.services.money import parse_money

router = APIRouter()


def _name(db: Session, user_id: int | None) -> str:
    user = db.get(User, user_id) if user_id else None
    return user.name if user else "알 수 없음"


def _approval_item(record: Resource, db: Session, viewer: User) -> dict[str, Any]:
    payload = record.payload
    result = {"id": str(record.id), **payload}
    result.setdefault("author", _name(db, record.owner_id))
    result.setdefault("authorId", str(record.owner_id or ""))
    result.setdefault("date", iso(record.created_at))
    result.setdefault("comments", [])
    result.setdefault("files", [])
    result.setdefault("line", [])
    result["received"] = any(
        int(step.get("userId", 0) or 0) == viewer.id
        and step.get("state") in {"now", "wait"}
        for step in result["line"]
    )
    # Keep internal workflow identifiers out of the public response.
    result.pop("approverIds", None)
    return result


def _approvers(
    db: Session, kind: str, amount: int, author_id: int | None = None
) -> list[User]:
    users = list(db.scalars(select(User).order_by(User.id)))
    eligible = [u for u in users if u.id != author_id]
    leads = [
        u
        for u in eligible
        if u.department == "NativeOps"
        and set(u.roles or []).intersection({"Leader", "Admin", "Lead", "부서 리드"})
    ]
    if not leads:
        leads = [u for u in eligible if "Admin" in (u.roles or [])]
    line = leads[:1]
    if kind in {"비용 승인", "프로젝트 요청", "운영 요청"} and amount >= 300000:
        fa = [u for u in eligible if set(u.roles or []).intersection({"FA", "Admin"})]
        if fa and (not line or fa[0].id != line[0].id):
            line.extend(fa[:1])
    return line


def _notify_approval(
    db: Session, record: Resource, user_id: int | None, title: str, action: bool = True
):
    notify_user(db, user_id, "내 업무", title, f"/inhouse/requests/{record.id}", action)


def _approval_files(db: Session, files: list[Any], user: User) -> list[dict[str, Any]]:
    normalized = []
    for value in files:
        if isinstance(value, dict):
            file_id = value.get("id", value.get("uploadId", value.get("fileId")))
            if not file_id and value.get("url"):
                file_id = str(value["url"]).split("/uploads/")[-1].split("?")[0]
        else:
            file_id = str(value).split("/uploads/")[-1].split("?")[0]
        if not file_id or not str(file_id).isdigit():
            raise HTTPException(422, "결재 첨부 파일 ID가 올바르지 않습니다.")
        upload = get_record(db, "upload", str(file_id))
        if not is_record_owner(upload, user):
            raise HTTPException(
                403, "본인이 업로드한 파일만 결재에 첨부할 수 있습니다."
            )
        normalized.append(
            {
                "id": str(upload.id),
                "url": f"/uploads/{upload.id}",
                "filename": upload.payload.get("filename", "file"),
                "size": int(upload.payload.get("size", 0)),
                "contentType": upload.payload.get("contentType", ""),
            }
        )
    return normalized


@router.get("/approvals", summary="결재 목록")
def get_approvals(
    box: str = "mine",
    status_filter: str | None = Query(None, alias="status"),
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if box not in {"mine", "received", "handled"}:
        raise HTTPException(400, "box는 mine, received, handled 중 하나여야 합니다.")
    stmt = select(Resource).where(Resource.resource_type == "approval")
    if box == "mine":
        stmt = stmt.where(Resource.owner_id == user.id)
    else:
        line = cast(Resource.payload["line"], JSONB)
        if box == "received":
            stmt = stmt.where(
                or_(
                    line.contains([{"userId": user.id, "state": "now"}]),
                    line.contains([{"userId": user.id, "state": "wait"}]),
                )
            )
        else:
            stmt = stmt.where(line.contains([{"userId": user.id, "state": "done"}]))
    if status_filter:
        stmt = stmt.where(Resource.payload["status"].as_string() == status_filter)
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.order_by(Resource.id.desc()).limit(limit + 1)))
    page = records[:limit]
    return envelope(
        {
            "items": [_approval_item(record, db, user) for record in page],
            "nextCursor": str(page[-1].id) if len(records) > limit and page else None,
        }
    )


@router.get("/approvals/{approval_id}", summary="결재 상세")
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "approval", approval_id)
    allowed = record.owner_id == user.id or any(
        int(step.get("userId", 0) or 0) == user.id
        for step in record.payload.get("line", [])
    )
    if not allowed:
        raise HTTPException(404, "결재를 찾을 수 없습니다.")
    return envelope(_approval_item(record, db, user))


class ApprovalCreate(BaseModel):
    kind: str
    type: str = ""
    title: str = Field(default="", max_length=200)
    amount: int | str = 0
    status: str = "결재 대기"
    project: str = ""
    useDate: str = ""
    reason: str = ""
    files: list[Any] = Field(default_factory=list)
    staff: dict[str, int] = Field(default_factory=dict)
    period: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    personnel: list[dict[str, Any]] = Field(default_factory=list)
    costType: str = ""

    model_config = ConfigDict(extra="allow")


class ApprovalPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    type: str | None = None
    amount: int | str | None = None
    status: str | None = None
    project: str | None = None
    useDate: str | None = None
    reason: str | None = None
    files: list[Any] | None = None
    staff: dict[str, int] | None = None
    period: str | None = None
    details: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    personnel: list[dict[str, Any]] | None = None
    costType: str | None = None

    model_config = ConfigDict(extra="forbid")


def _parse_amount(value: Any) -> int:
    try:
        amount = parse_money(value)
    except ValueError:
        raise HTTPException(422, "금액 형식이 올바르지 않습니다.") from None
    if amount < 0:
        raise HTTPException(422, "금액은 0 이상의 유효한 숫자여야 합니다.")
    return amount


def _nonnegative_money(value: Any, field: str) -> int:
    try:
        amount = parse_money(value)
    except ValueError:
        raise HTTPException(422, f"{field} 금액 형식이 올바르지 않습니다.") from None
    if amount < 0:
        raise HTTPException(422, f"{field} 금액은 0 이상이어야 합니다.")
    return amount


@router.post("/approvals", status_code=201, summary="결재 작성 또는 임시 저장")
def create_approval(
    payload: ApprovalCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    kind_aliases = {
        "cost": "비용 승인",
        "expense": "비용 승인",
        "비용": "비용 승인",
        "비용 승인 요청": "비용 승인",
        "비용 승인": "비용 승인",
        "project": "인력 지원",
        "staff": "인력 지원",
        "project_staffing": "인력 지원",
        "프로젝트 인력 지원": "인력 지원",
        "인력 지원": "인력 지원",
        "project_request": "프로젝트 요청",
        "project-request": "프로젝트 요청",
        "프로젝트 요청": "프로젝트 요청",
        "프로젝트 관련 요청서": "프로젝트 요청",
        "operating": "운영 요청",
        "operations": "운영 요청",
        "운영 요청": "운영 요청",
        "운영 관련 요청서": "운영 요청",
        "internal": "기타 내부 문서",
        "기타 내부 문서": "기타 내부 문서",
    }
    kind = kind_aliases.get(payload.kind)
    if kind is None:
        raise HTTPException(422, "지원하지 않는 결재 문서 유형입니다.")
    draft = payload.status in {"작성 중", "draft", "DRAFT"}
    if not draft and not payload.title.strip():
        raise HTTPException(422, "결재 제목을 입력해주세요.")
    if (
        not draft
        and kind == "인력 지원"
        and not payload.staff
        and not payload.personnel
    ):
        raise HTTPException(422, "지원할 직군과 인원을 입력해주세요.")
    if (
        not draft
        and kind in {"프로젝트 요청", "운영 요청", "기타 내부 문서"}
        and not payload.reason.strip()
    ):
        raise HTTPException(422, "요청 사유를 입력해주세요.")
    amount_source = payload.amount or payload.budget.get("amount", 0)
    amount = _parse_amount(amount_source)
    line = [
        {
            "who": user.name,
            "userId": user.id,
            "role": "기안",
            "state": "done",
            "at": iso(datetime.now().astimezone()),
        }
    ]
    approvers = [] if draft else _approvers(db, kind, amount, user.id)
    if not draft and not approvers:
        raise HTTPException(
            409,
            "결재를 제출하려면 본인 외의 NativeOps 리드 또는 관리자 계정이 필요합니다.",
        )
    role_labels = ["검토", "승인"]
    for index, approver in enumerate(approvers):
        line.append(
            {
                "who": approver.name,
                "userId": approver.id,
                "role": role_labels[min(index, 1)],
                "state": "now" if index == 0 else "wait",
                "at": "대기",
            }
        )
    status_value = "작성 중" if draft else ("검토 중" if approvers else "결재 대기")
    record = create_record(
        db,
        "approval",
        {
            **{
                key: value
                for key, value in payload.model_dump().items()
                if key not in {"line", "approverIds", "author", "authorId", "received"}
            },
            "kind": kind,
            "type": payload.type
            or (
                ", ".join(f"{role} {count}명" for role, count in payload.staff.items())
                if kind == "인력 지원"
                else "기타"
            ),
            "title": payload.title,
            "amount": payload.period if kind == "인력 지원" else f"{amount:,}원",
            "amountValue": amount,
            "status": status_value,
            "project": payload.project,
            "useDate": payload.useDate,
            "reason": payload.reason,
            "line": line,
            "files": _approval_files(db, payload.files, user),
            "comments": [],
            "approverIds": [u.id for u in approvers],
            "staff": payload.staff,
            "period": payload.period,
        },
        user,
    )
    if approvers:
        _notify_approval(db, record, approvers[0].id, f"새 결재 요청: {payload.title}")
    return envelope(
        {"id": str(record.id), "status": status_value}, "결재를 저장했습니다.", 201
    )


@router.patch("/approvals/{approval_id}", summary="결재 수정")
def patch_approval(
    approval_id: str,
    payload: ApprovalPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "approval", approval_id)
    if record.owner_id != user.id or record.payload.get("status") not in {
        "작성 중",
        "반려",
    }:
        raise HTTPException(403, "작성 중이거나 반려된 본인 결재만 수정할 수 있습니다.")
    changes = payload.model_dump(exclude_unset=True)
    requested_status = changes.get("status")
    if requested_status not in {None, "작성 중", "결재 대기"}:
        raise HTTPException(
            422, "결재 상태는 작성 중 또는 결재 제출만 설정할 수 있습니다."
        )
    data = {**record.payload, **changes}
    if "files" in changes:
        data["files"] = _approval_files(db, list(changes.get("files") or []), user)
    if changes.get("status") == "결재 대기":
        if not str(data.get("title", "")).strip():
            raise HTTPException(422, "결재 제목을 입력해주세요.")
        if (
            data.get("kind") == "인력 지원"
            and not data.get("staff")
            and not data.get("personnel")
        ):
            raise HTTPException(422, "지원할 직군과 인원을 입력해주세요.")
        if (
            data.get("kind") in {"프로젝트 요청", "운영 요청", "기타 내부 문서"}
            and not str(data.get("reason", "")).strip()
        ):
            raise HTTPException(422, "요청 사유를 입력해주세요.")
        amount_source = data.get("amount") or (data.get("budget") or {}).get(
            "amount", 0
        )
        amount_value = (
            0 if data.get("kind") == "인력 지원" else _parse_amount(amount_source)
        )
        approvers = _approvers(db, data.get("kind", "비용 승인"), amount_value, user.id)
        if not approvers:
            raise HTTPException(409, "결재선을 구성할 다른 담당자 계정이 없습니다.")
        data["amountValue"] = amount_value
        if data.get("kind") != "인력 지원":
            data["amount"] = f"{amount_value:,}원"
        data["approverIds"] = [a.id for a in approvers]
        data["line"] = [
            {
                "who": user.name,
                "userId": user.id,
                "role": "기안",
                "state": "done",
                "at": iso(datetime.now().astimezone()),
            }
        ] + [
            {
                "who": a.name,
                "userId": a.id,
                "role": "검토" if i == 0 else "승인",
                "state": "now" if i == 0 else "wait",
                "at": "대기",
            }
            for i, a in enumerate(approvers)
        ]
        data["status"] = "검토 중"
        data.pop("rejectReason", None)
    elif changes.get("status") == "작성 중":
        data["status"] = "작성 중"
        data["approverIds"] = []
        data["line"] = [
            {
                "who": user.name,
                "userId": user.id,
                "role": "기안",
                "state": "done",
                "at": iso(datetime.now().astimezone()),
            }
        ]
        data.pop("rejectReason", None)
    update_record(db, record, data)
    first = (
        (data.get("approverIds") or [None])[0]
        if changes.get("status") == "결재 대기"
        else None
    )
    if first:
        _notify_approval(db, record, first, f"새 결재 요청: {data.get('title', '')}")
    return envelope(_approval_item(record, db, user), "결재를 수정했습니다.")


def _current_approver(record: Resource, user: User) -> tuple[int, dict[str, Any]]:
    for index, step in enumerate(record.payload.get("line", [])):
        if int(step.get("userId", 0) or 0) == user.id and step.get("state") == "now":
            return index, step
    raise HTTPException(403, "현재 결재 차례가 아닙니다.")


def _decide(approval_id: str, action: str, reason: str | None, db: Session, user: User):
    record = get_record(db, "approval", approval_id)
    if record.payload.get("status") not in {"결재 대기", "검토 중"}:
        raise HTTPException(409, "처리할 수 있는 결재 상태가 아닙니다.")
    index, current = _current_approver(record, user)
    line = [dict(x) for x in record.payload.get("line", [])]
    line[index].update({"state": "done", "at": iso(datetime.now().astimezone())})
    data = {**record.payload, "line": line}
    if action == "reject":
        if not reason or not reason.strip():
            raise HTTPException(400, "반려 사유를 입력해주세요.")
        data.update({"status": "반려", "rejectReason": reason.strip()})
        _notify_approval(
            db,
            record,
            record.owner_id,
            f"결재가 반려됐어요: {data.get('title', '')}",
            False,
        )
    else:
        next_step = next(
            (i for i, step in enumerate(line) if step.get("state") == "wait"), None
        )
        if next_step is None:
            data["status"] = "승인"
            _notify_approval(
                db,
                record,
                record.owner_id,
                f"결재가 승인됐어요: {data.get('title', '')}",
                False,
            )
        else:
            line[next_step].update({"state": "now", "at": "검토 중"})
            data["line"] = line
            data["status"] = "검토 중"
            _notify_approval(
                db,
                record,
                int(line[next_step].get("userId")),
                f"결재 요청이 도착했어요: {data.get('title', '')}",
            )
    update_record(db, record, data)
    return envelope(
        {"id": str(record.id), "status": data["status"]}, "결재를 처리했습니다."
    )


@router.post("/approvals/{approval_id}/approve", summary="결재 승인")
def approve(
    approval_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _decide(approval_id, "approve", None, db, user)


@router.post("/approvals/{approval_id}/reject", summary="결재 반려")
def reject(
    approval_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _decide(approval_id, "reject", payload.get("reason"), db, user)


@router.post("/approvals/{approval_id}/cancel", summary="결재 취소")
def cancel(
    approval_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "approval", approval_id)
    if record.owner_id != user.id or record.payload.get("status") not in {
        "작성 중",
        "결재 대기",
        "검토 중",
    }:
        raise HTTPException(403, "취소할 수 없는 결재입니다.")
    data = {**record.payload, "status": "취소"}
    update_record(db, record, data)
    for step in record.payload.get("line", []):
        if step.get("state") in {"now", "wait"}:
            _notify_approval(
                db,
                record,
                int(step.get("userId", 0) or 0),
                f"결재가 취소됐어요: {record.payload.get('title', '')}",
                False,
            )
    return envelope({"id": str(record.id), "status": "취소"}, "결재를 취소했습니다.")


@router.post("/approvals/{approval_id}/comments", status_code=201, summary="결재 댓글")
def add_approval_comment(
    approval_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "approval", approval_id)
    authorized = record.owner_id == user.id or any(
        int(s.get("userId", 0) or 0) == user.id for s in record.payload.get("line", [])
    )
    if not authorized:
        raise HTTPException(404, "결재를 찾을 수 없습니다.")
    text = payload.get("text", "").strip()
    if not text:
        raise HTTPException(400, "댓글 내용을 입력해주세요.")
    comment = {
        "id": secrets.token_hex(8),
        "who": user.name,
        "whoId": str(user.id),
        "meta": user.department or "",
        "text": text,
        "createdAt": iso(datetime.now().astimezone()),
    }
    record.payload = {
        **record.payload,
        "comments": [*record.payload.get("comments", []), comment],
    }
    db.commit()
    return envelope(comment, "댓글을 등록했습니다.", 201)


@router.get("/finance/summary", summary="재무 요약")
def finance_summary(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    require_finance(user)
    receipts = list(
        db.scalars(select(Resource).where(Resource.resource_type == "finance_receipt"))
    )
    approvals = list(
        db.scalars(select(Resource).where(Resource.resource_type == "approval"))
    )
    bank_expenses = list(
        db.scalars(
            select(Resource).where(Resource.resource_type == "finance_bank_expense")
        )
    )
    current_month = date.today().strftime("%Y-%m")
    collected = [r for r in receipts if r.payload.get("status") == "입금 완료"]
    month_income = sum(
        int(r.payload.get("amount", 0))
        for r in collected
        if str(r.payload.get("date", "")).startswith(current_month)
    )
    month_expense = sum(
        int(a.payload.get("amountValue", 0) or 0)
        for a in approvals
        if a.payload.get("status") == "승인"
        and str(a.payload.get("useDate", "")).startswith(current_month)
    )
    month_expense += sum(
        int(e.payload.get("amount", 0) or 0)
        for e in bank_expenses
        if str(e.payload.get("date", "")).startswith(current_month)
    )
    settings_row = db.scalar(
        select(Resource).where(Resource.resource_type == "finance_settings")
    )
    finance = settings_row.payload if settings_row else {}
    pools = finance.get(
        "pools",
        [
            {
                "id": "operation",
                "label": "운영풀",
                "amount": 0,
                "ratio": 60,
                "color": "#3182f6",
                "note": "운영 자금",
            },
            {
                "id": "reserve",
                "label": "적립풀",
                "amount": 0,
                "ratio": 30,
                "color": "#7c5cff",
                "note": "예비 자금",
            },
            {
                "id": "dividend",
                "label": "배당풀",
                "amount": 0,
                "ratio": 10,
                "color": "#ff8a3d",
                "note": "참여자 배당",
            },
        ],
    )
    available = sum(
        int(pool.get("amount", 0)) for pool in pools if pool.get("id") == "operation"
    )
    reserve = sum(
        int(pool.get("amount", 0)) for pool in pools if pool.get("id") == "reserve"
    )
    flow = finance.get("flow", [])
    if not flow:
        flow_months = sorted(
            {
                str(r.payload.get("date", ""))[:7]
                for r in collected + bank_expenses
                if str(r.payload.get("date", ""))[:7]
            }
        )
        flow = [
            {
                "month": month,
                "income": sum(
                    int(r.payload.get("amount", 0))
                    for r in collected
                    if str(r.payload.get("date", "")).startswith(month)
                ),
                "expense": sum(
                    int(e.payload.get("amount", 0))
                    for e in bank_expenses
                    if str(e.payload.get("date", "")).startswith(month)
                ),
            }
            for month in flow_months
        ]
    last_synced = db.scalar(
        select(BankSyncRun.finished_at)
        .where(BankSyncRun.status == "success", BankSyncRun.finished_at.is_not(None))
        .order_by(BankSyncRun.id.desc())
        .limit(1)
    )
    return envelope(
        {
            "period": date.today().strftime("%Y년 %m월"),
            "demo": False,
            "lastSyncedAt": last_synced.isoformat() if last_synced else None,
            "summary": {
                "available": available,
                "monthlyRevenue": month_income,
                "monthlyExpense": month_expense,
                "externalReceipts": sum(
                    int(r.payload.get("amount", 0)) for r in receipts
                ),
                "reserveBalance": reserve,
            },
            "pools": pools,
            "flow": flow,
            "receipts": [
                {
                    k: r.payload.get(k)
                    for k in ("client", "project", "amount", "status", "date")
                }
                for r in receipts[:10]
            ],
            "notices": finance.get("notices", []),
            "member": {"email": user.email, "name": user.name},
        }
    )


@router.get("/finance/receipts", summary="수금 내역")
def get_receipts(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_finance(user)
    for value in (from_date, to_date):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(
                    400, "from과 to는 YYYY-MM-DD 형식이어야 합니다."
                ) from None
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "from은 to보다 늦을 수 없습니다.")
    base_filters = [Resource.resource_type == "finance_receipt"]
    if from_date:
        base_filters.append(Resource.payload["date"].as_string() >= from_date)
    if to_date:
        base_filters.append(Resource.payload["date"].as_string() <= to_date)
    amount = cast(Resource.payload["amount"].as_string(), Numeric(20, 0))
    summary_rows = db.execute(
        select(Resource.payload["status"].as_string(), func.sum(amount))
        .where(*base_filters)
        .group_by(Resource.payload["status"].as_string())
    )
    totals = {"totalOrder": 0, "paid": 0, "pending": 0, "overdue": 0}
    for status, amount_total in summary_rows:
        amount_total = int(amount_total or 0)
        totals["totalOrder"] += amount_total
        if status == "입금 완료":
            totals["paid"] += amount_total
        elif status in {"입금 대기", "청구 완료"}:
            totals["pending"] += amount_total
        elif status == "연체":
            totals["overdue"] += amount_total

    stmt = select(Resource).where(*base_filters)
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
            "totals": totals,
        }
    )


@router.get("/finance/contracts/{project}", summary="계약 상세")
def get_contract(
    project: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    require_finance(user)
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "finance_contract",
            (Resource.payload["projectId"].as_string() == project)
            | (Resource.payload["project"].as_string() == project),
        )
    )
    if record is None:
        raise HTTPException(404, "계약 정보를 찾을 수 없습니다.")
    return envelope(record.payload)


@router.post("/admin/finance/receipts", status_code=201, summary="수금 등록")
def create_receipt(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if "Admin" not in (user.roles or []) and "FA" not in (user.roles or []):
        raise HTTPException(403, "수금 등록 권한이 없습니다.")
    for key in ("client", "project", "amount", "status", "date"):
        if key not in payload:
            raise HTTPException(400, f"{key} 항목은 필수입니다.")
    data = {**payload, "amount": _nonnegative_money(payload["amount"], "수금")}
    record = create_record(db, "finance_receipt", data, user)
    return envelope(
        {"id": str(record.id), **record.payload}, "수금 내역을 등록했습니다.", 201
    )


@router.patch("/admin/finance/receipts/{receipt_id}", summary="수금 상태 수정")
def patch_receipt(
    receipt_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if "Admin" not in (user.roles or []) and "FA" not in (user.roles or []):
        raise HTTPException(403, "수금 변경 권한이 없습니다.")
    record = get_record(db, "finance_receipt", receipt_id)
    changes = dict(payload)
    if "amount" in changes:
        changes["amount"] = _nonnegative_money(changes["amount"], "수금")
    updated = update_record(db, record, {**record.payload, **changes})
    return envelope(
        {"id": str(updated.id), **updated.payload}, "수금 내역을 수정했습니다."
    )


@router.post("/admin/finance/contracts", status_code=201, summary="계약 정보 등록")
def create_contract(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if "Admin" not in (user.roles or []) and "FA" not in (user.roles or []):
        raise HTTPException(403, "계약 정보 등록 권한이 없습니다.")
    project = payload.get("project") or payload.get("projectId")
    if not project:
        raise HTTPException(400, "프로젝트 식별 정보가 필요합니다.")
    existing = db.scalar(
        select(Resource).where(
            Resource.resource_type == "finance_contract",
            Resource.payload["projectId"].as_string() == str(project),
        )
    )
    if existing:
        raise HTTPException(409, "해당 프로젝트의 계약 정보가 이미 등록되어 있습니다.")
    data = dict(payload)
    for key in ("totalAmount", "amount", "total", "collectedAmount", "collected"):
        if key in data and data[key] is not None:
            data[key] = _nonnegative_money(data[key], "계약")
    record = create_record(db, "finance_contract", data, user)
    return envelope(
        {"id": str(record.id), **record.payload}, "계약 정보를 등록했습니다.", 201
    )


@router.patch("/admin/finance/contracts/{project}", summary="계약 정보 수정")
def patch_contract(
    project: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if "Admin" not in (user.roles or []) and "FA" not in (user.roles or []):
        raise HTTPException(403, "계약 정보 수정 권한이 없습니다.")
    contract = db.scalar(
        select(Resource).where(
            Resource.resource_type == "finance_contract",
            (Resource.payload["projectId"].as_string() == project)
            | (Resource.payload["project"].as_string() == project),
        )
    )
    if contract is None:
        raise HTTPException(404, "계약 정보를 찾을 수 없습니다.")
    changes = dict(payload)
    for key in ("totalAmount", "amount", "total", "collectedAmount", "collected"):
        if key in changes and changes[key] is not None:
            changes[key] = _nonnegative_money(changes[key], "계약")
    updated = update_record(db, contract, {**contract.payload, **changes})
    return envelope(updated.payload, "계약 정보를 수정했습니다.")


@router.get("/admin/finance/settings", summary="재무 요약 설정 조회")
def get_finance_settings(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if "Admin" not in (user.roles or []) and "FA" not in (user.roles or []):
        raise HTTPException(403, "재무 설정 권한이 없습니다.")
    record = db.scalar(
        select(Resource).where(Resource.resource_type == "finance_settings")
    )
    return envelope(
        record.payload if record else {"pools": [], "flow": [], "notices": []}
    )


@router.patch("/admin/finance/settings", summary="재무 요약 설정 수정")
def patch_finance_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if "Admin" not in (user.roles or []) and "FA" not in (user.roles or []):
        raise HTTPException(403, "재무 설정 권한이 없습니다.")
    record = db.scalar(
        select(Resource).where(Resource.resource_type == "finance_settings")
    )
    if record:
        changes = dict(payload)
        if "pools" in changes:
            changes["pools"] = [
                {
                    **pool,
                    "amount": _nonnegative_money(pool.get("amount", 0), "풀"),
                }
                for pool in changes["pools"]
            ]
        data = update_record(db, record, {**record.payload, **changes})
    else:
        changes = dict(payload)
        if "pools" in changes:
            changes["pools"] = [
                {
                    **pool,
                    "amount": _nonnegative_money(pool.get("amount", 0), "풀"),
                }
                for pool in changes["pools"]
            ]
        data = create_record(db, "finance_settings", changes)
    return envelope(data.payload, "재무 설정을 저장했습니다.")
