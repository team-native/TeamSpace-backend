from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    iso,
    user_can_finance,
)
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User

router = APIRouter()


def _require_doc_access(data: dict, user: User) -> None:
    if (
        str(data.get("type", "")).upper() == "FINANCE"
        or str(data.get("category", "")).upper() == "FINANCE"
    ) and not user_can_finance(user):
        raise HTTPException(403, "재무 문서를 열람할 권한이 없습니다.")


def _doc(db: Session, slug: str) -> Resource:
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "doc",
            Resource.payload["slug"].as_string() == slug,
        )
    )
    if record is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return record


@router.get("/docs", summary="문서함 목록")
def get_docs(
    q: str | None = None,
    category: str | None = None,
    type: str | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Resource).where(Resource.resource_type == "doc")
    if not user_can_finance(user):
        finance_doc = or_(
            func.upper(func.coalesce(Resource.payload["type"].as_string(), ""))
            == "FINANCE",
            func.upper(func.coalesce(Resource.payload["category"].as_string(), ""))
            == "FINANCE",
        )
        stmt = stmt.where(~finance_doc)
    if category:
        stmt = stmt.where(Resource.payload["category"].as_string() == category)
    if type:
        stmt = stmt.where(Resource.payload["type"].as_string() == type)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Resource.payload["title"].as_string().ilike(pattern),
                Resource.payload["copy"].as_string().ilike(pattern),
            )
        )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.order_by(Resource.id.desc()).limit(limit + 1)))
    page = records[:limit]
    docs = []
    for record in page:
        data = record.payload
        docs.append(
            {
                k: data.get(k)
                for k in (
                    "slug",
                    "category",
                    "type",
                    "title",
                    "copy",
                    "date",
                    "version",
                    "effective",
                    "owner",
                    "revised",
                )
                if k in data
            }
        )
    return envelope(
        {
            "items": docs,
            "nextCursor": str(page[-1].id) if len(records) > limit and page else None,
        }
    )


@router.get("/docs/{slug}", summary="문서 상세")
def get_doc(
    slug: str,
    version: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = _doc(db, slug)
    data = record.payload
    if version and version != data.get("version"):
        history = db.scalar(
            select(Resource).where(
                Resource.resource_type == "doc_revision",
                Resource.payload["slug"].as_string() == slug,
                Resource.payload["version"].as_string() == version,
            )
        )
        if history is None:
            raise HTTPException(404, "요청한 문서 버전을 찾을 수 없습니다.")
        data = history.payload.get("document", data)
    _require_doc_access(data, user)
    result = {**data}
    if data.get("type") == "TEMPLATE":
        result.setdefault("fields", data.get("fields", []))
        if data.get("fileId"):
            result["downloadUrl"] = f"/uploads/{data['fileId']}"
    return envelope(result)


@router.get("/docs/{slug}/history", summary="문서 개정 이력")
def get_doc_history(
    slug: str,
    version: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    document = _doc(db, slug)
    _require_doc_access(document.payload, user)
    if version:
        chosen = db.scalar(
            select(Resource).where(
                Resource.resource_type == "doc_revision",
                Resource.payload["slug"].as_string() == slug,
                or_(
                    Resource.payload["ver"].as_string() == version,
                    Resource.payload["version"].as_string() == version,
                ),
            )
        )
        if chosen is None:
            raise HTTPException(404, "문서 버전을 찾을 수 없습니다.")
        data = chosen.payload.get("document", chosen.payload)
        _require_doc_access(data, user)
        return envelope(data)
    stmt = select(Resource).where(
        Resource.resource_type == "doc_revision",
        Resource.payload["slug"].as_string() == slug,
    )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.order_by(Resource.id.desc()).limit(limit + 1)))
    page = records[:limit]
    rows = [
        {k: record.payload.get(k) for k in ("ver", "date", "who", "note")}
        for record in page
    ]
    return envelope(
        {
            "items": rows,
            "nextCursor": str(page[-1].id) if len(records) > limit and page else None,
        }
    )


@router.post("/docs/{slug}/read", summary="문서 읽음 확인")
def read_doc(
    slug: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    document = _doc(db, slug)
    _require_doc_access(document.payload, user)
    receipt = db.scalar(
        select(Resource).where(
            Resource.resource_type == "doc_read",
            Resource.owner_id == user.id,
            Resource.payload["slug"].as_string() == slug,
        )
    )
    if receipt is None:
        create_record(
            db,
            "doc_read",
            {"slug": slug, "readAt": iso(datetime.now().astimezone())},
            user,
        )
    from sqlalchemy import func

    total = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "doc_read",
                Resource.payload["slug"].as_string() == slug,
            )
        )
        or 0
    )
    return envelope({"slug": slug, "read": True, "confirmed": total})
