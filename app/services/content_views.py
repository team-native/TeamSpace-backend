from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.content_view import ContentView
from app.models.resource import Resource
from app.models.user import User


def count_content_view(
    db: Session, resource_type: str, record: Resource, user: User
) -> Resource:
    locked_record = db.scalar(
        select(Resource).where(Resource.id == record.id).with_for_update()
    )
    if locked_record is None:
        return record

    result = db.execute(
        insert(ContentView)
        .values(
            resource_type=resource_type,
            resource_id=str(locked_record.id),
            user_id=user.id,
        )
        .on_conflict_do_nothing(constraint="uq_content_views_resource_user")
    )
    if result.rowcount:
        try:
            views = max(0, int(locked_record.payload.get("views", 0) or 0)) + 1
        except (TypeError, ValueError):
            views = 1
        locked_record.payload = {**locked_record.payload, "views": views}
    db.commit()
    return locked_record
