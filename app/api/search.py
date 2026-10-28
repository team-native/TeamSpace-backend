from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.api.dependencies import get_current_user
from app.api.shared import envelope, iso, user_can_finance
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User

router = APIRouter()

SEARCH_TYPES = {
    "ALL": {"doc", "rule", "project", "person", "post", "proposal"},
    "DOC": {"doc", "rule"},
    "DOCS": {"doc", "rule"},
    "DOCUMENT": {"doc", "rule"},
    "DOCUMENTS": {"doc", "rule"},
    "RULE": {"rule", "doc"},
    "PROJECT": {"project"},
    "PEOPLE": {"person"},
    "PERSON": {"person"},
    "POST": {"post"},
    "PROPOSAL": {"proposal"},
}


@router.get("/search", summary="권한에 따른 통합 검색")
def search(
    query: str | None = Query(None, min_length=2),
    q: str | None = Query(None, min_length=2),
    type: str = "ALL",
    period: str = "all",
    dept: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    needle = (query or q or "").strip()
    if len(needle) < 2:
        raise HTTPException(422, "검색어는 2자 이상 입력해주세요.")
    kinds = SEARCH_TYPES.get(type.upper())
    if kinds is None:
        raise HTTPException(400, "지원하지 않는 검색 유형입니다.")

    try:
        offset = int(cursor or 0)
    except ValueError:
        raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    if offset < 0 or offset > 10_000:
        raise HTTPException(400, "cursor 값이 허용 범위를 벗어났습니다.")
    take = offset + limit + 1
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period)
    if period != "all" and days is None:
        raise HTTPException(400, "period는 all, 7d, 30d, 90d 중 하나여야 합니다.")
    created_after = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    pattern = f"%{needle}%"
    matches = []
    counts_by_kind: dict[str, int] = {}
    for kind in kinds:
        if kind == "person":
            profile = aliased(Resource)
            profile_dept = func.coalesce(
                profile.payload["dept"].as_string(), User.department, ""
            )
            person_filters = []
            if dept:
                person_filters.append(profile_dept == dept)
            if created_after:
                person_filters.append(User.created_at >= created_after)
            person_filters.append(
                or_(
                    User.name.ilike(pattern),
                    User.email.ilike(pattern),
                    profile_dept.ilike(pattern),
                    profile.payload["major"].as_string().ilike(pattern),
                    profile.payload["bio"].as_string().ilike(pattern),
                )
            )
            join_condition = and_(
                profile.resource_type == "profile", profile.owner_id == User.id
            )
            count = (
                db.scalar(
                    select(func.count(func.distinct(User.id)))
                    .select_from(User)
                    .outerjoin(profile, join_condition)
                    .where(*person_filters)
                )
                or 0
            )
            counts_by_kind[kind] = count
            title_match = func.lower(User.name).contains(needle.casefold())
            people = db.execute(
                select(User, profile)
                .outerjoin(profile, join_condition)
                .where(*person_filters)
                .order_by(title_match.desc(), User.created_at.desc(), User.id.desc())
                .limit(take)
            )
            for person, profile_row in people:
                profile_data = profile_row.payload if profile_row else {}
                person_dept = profile_data.get("dept", person.department or "")
                role = profile_data.get("major", "")
                matches.append(
                    {
                        "category": "people",
                        "id": str(person.id),
                        "title": person.name,
                        "snippet": " · ".join(
                            value for value in (person_dept, role) if value
                        ),
                        "createdAt": iso(person.created_at),
                        "href": f"/inhouse/people/{person.id}",
                    }
                )
            continue

        resource_type = kind
        title_expr = func.coalesce(
            Resource.payload["title"].as_string(),
            Resource.payload["name"].as_string(),
            "",
        )
        content_expr = func.coalesce(
            Resource.payload["copy"].as_string(),
            Resource.payload["body"].as_string(),
            Resource.payload["description"].as_string(),
            Resource.payload["content"].as_string(),
            "",
        )
        filters = [Resource.resource_type == resource_type]
        if created_after:
            filters.append(Resource.created_at >= created_after)
        filters.append(or_(title_expr.ilike(pattern), content_expr.ilike(pattern)))
        if kind == "post":
            filters.extend(
                [
                    func.coalesce(Resource.payload["status"].as_string(), "published")
                    == "published",
                    or_(
                        Resource.payload["anonymous"].as_boolean().is_(False),
                        Resource.payload["anonymous"].as_boolean().is_(None),
                    ),
                    or_(
                        Resource.payload["isAnonymous"].as_boolean().is_(False),
                        Resource.payload["isAnonymous"].as_boolean().is_(None),
                    ),
                ]
            )
        if kind == "proposal":
            filters.extend(
                [
                    or_(
                        Resource.payload["anonymous"].as_boolean().is_(False),
                        Resource.payload["anonymous"].as_boolean().is_(None),
                    ),
                    or_(
                        Resource.payload["isAnonymous"].as_boolean().is_(False),
                        Resource.payload["isAnonymous"].as_boolean().is_(None),
                    ),
                ]
            )
        if kind in {"doc", "rule"} and not user_can_finance(user):
            filters.append(
                and_(
                    func.upper(func.coalesce(Resource.payload["type"].as_string(), ""))
                    != "FINANCE",
                    func.upper(
                        func.coalesce(Resource.payload["category"].as_string(), "")
                    )
                    != "FINANCE",
                )
            )
        dept_expr = func.coalesce(
            Resource.payload["dept"].as_string(),
            Resource.payload["department"].as_string(),
            "",
        )
        if dept and kind == "project":
            filters.append(dept_expr == dept)
        count = (
            db.scalar(select(func.count()).select_from(Resource).where(*filters)) or 0
        )
        counts_by_kind[kind] = count
        title_match = func.lower(title_expr).contains(needle.casefold())
        records = db.scalars(
            select(Resource)
            .where(*filters)
            .order_by(
                title_match.desc(), Resource.created_at.desc(), Resource.id.desc()
            )
            .limit(take)
        )
        for record in records:
            data = record.payload or {}
            doc_type = str(data.get("type", data.get("category", ""))).upper()
            record_dept = str(data.get("dept", data.get("department", "")))
            title = str(data.get("title", data.get("name", "")))
            content = str(
                data.get(
                    "copy",
                    data.get("body", data.get("description", data.get("content", ""))),
                )
            )
            category = {
                "doc": "documents",
                "rule": "documents",
                "project": "projects",
                "post": "posts",
                "proposal": "proposals",
            }[kind]
            item = {
                "category": category,
                "id": str(record.id),
                "title": title,
                "snippet": content[:160],
                "createdAt": iso(record.created_at),
                "href": {
                    "doc": f"/inhouse/documents/{data.get('slug', record.id)}",
                    "rule": f"/inhouse/documents/{data.get('slug', record.id)}",
                    "project": f"/inhouse/projects/{data.get('slug', record.id)}",
                    "post": f"/inhouse/community/{record.id}",
                    "proposal": f"/inhouse/community/proposals/{record.id}",
                }[kind],
            }
            if kind in {"doc", "rule"}:
                item["type"] = doc_type
                item["version"] = data.get("version")
            elif kind == "post":
                item["author"] = data.get("author", "")
                item["comments"] = int(data.get("commentCount", 0) or 0)
            elif kind == "proposal":
                item["status"] = data.get("status", "")
                item["likes"] = int(
                    data.get("likes", data.get("recommendations", 0)) or 0
                )
            elif kind == "project":
                item["dept"] = record_dept
                item["pm"] = data.get("pm", "")
                item["progress"] = data.get("progress", 0)
            matches.append(item)

    matches.sort(
        key=lambda item: (
            needle.casefold() in item["title"].casefold(),
            item.get("createdAt") or "",
        ),
        reverse=True,
    )
    page = matches[offset : offset + limit]
    groups: dict[str, list] = {
        "documents": [],
        "projects": [],
        "people": [],
        "posts": [],
        "proposals": [],
    }
    for item in page:
        groups[item["category"]].append(item)
    counts = {
        "documents": counts_by_kind.get("doc", 0) + counts_by_kind.get("rule", 0),
        "projects": counts_by_kind.get("project", 0),
        "people": counts_by_kind.get("person", 0),
        "posts": counts_by_kind.get("post", 0),
        "proposals": counts_by_kind.get("proposal", 0),
    }
    return envelope(
        {
            "query": needle,
            "groups": groups,
            "counts": counts,
            "items": page,
            "result": page,
            "nextCursor": str(offset + limit)
            if offset + limit < len(matches)
            else None,
        }
    )
