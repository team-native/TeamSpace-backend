import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.resource import Resource
from app.models.user import User

logger = logging.getLogger(__name__)


def _parse_publish_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_notice_visible(payload: dict, now: datetime | None = None) -> bool:
    status = payload.get("status", "PUBLISHED")
    if status == "PUBLISHED":
        return True
    if status != "SCHEDULED":
        return False
    publish_at = _parse_publish_at(payload.get("publishAt"))
    if publish_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    return publish_at <= current.astimezone(timezone.utc)


def publish_due_notices(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    due: list[tuple[str, str, bool]] = []

    with SessionLocal() as db:
        records = list(
            db.scalars(
                select(Resource)
                .where(
                    Resource.resource_type == "notice",
                    Resource.payload["status"].as_string() == "SCHEDULED",
                )
                .with_for_update(skip_locked=True)
            )
        )
        for record in records:
            publish_at = _parse_publish_at(record.payload.get("publishAt"))
            if publish_at is None:
                logger.error("Scheduled notice %s has invalid publishAt", record.id)
                continue
            if publish_at > current:
                continue
            payload = dict(record.payload)
            payload["status"] = "PUBLISHED"
            payload["publishedAt"] = current.isoformat()
            record.payload = payload
            due.append(
                (
                    str(record.id),
                    str(payload.get("title", "")),
                    payload.get("sendPush", True) is not False,
                )
            )
        if due:
            db.commit()

    for notice_id, title, send_push in due:
        if not send_push:
            continue
        _announce_notice(notice_id, title)
    return len(due)


def _announce_notice(notice_id: str, title: str) -> None:
    from app.api.shared import notify_user

    with SessionLocal() as db:
        user_ids = list(db.scalars(select(User.id)))
        for user_id in user_ids:
            try:
                notify_user(
                    db,
                    user_id,
                    "공지",
                    f"새 공지: {title}",
                    f"/inhouse/community/notices/{notice_id}",
                )
            except Exception:
                logger.exception(
                    "Could not notify user %s about scheduled notice %s",
                    user_id,
                    notice_id,
                )


async def scheduled_notice_worker(poll_interval: float = 15) -> None:
    while True:
        try:
            await asyncio.to_thread(publish_due_notices)
        except Exception:
            logger.exception("Scheduled notice processing failed")
        await asyncio.sleep(poll_interval)
