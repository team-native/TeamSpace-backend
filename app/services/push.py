import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.shared import _is_do_not_disturb
from app.core.config import settings
from app.models.resource import Resource

logger = logging.getLogger(__name__)


def _push_allowed(config: dict, kind: str) -> bool:
    if config.get("push", True) is False:
        return False
    kinds = config.get("kinds") or {}
    if kind in kinds and not kinds[kind]:
        return False
    return not _is_do_not_disturb(config)


def send_push(
    db: Session,
    user_id: int,
    notification: dict,
    preferences: dict | None = None,
) -> None:
    if not (settings.vapid_public_key and settings.vapid_private_key):
        return
    if preferences is None:
        settings_row = db.scalar(
            select(Resource).where(
                Resource.resource_type == "notification_settings",
                Resource.owner_id == user_id,
            )
        )
        preferences = settings_row.payload if settings_row else {}
    kind = notification.get("sourceKind", notification.get("kind", ""))
    if not _push_allowed(preferences, kind):
        return
    subscriptions = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "push_subscription",
                Resource.owner_id == user_id,
            )
        )
    )
    if not subscriptions:
        return
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning(
            "pywebpush is missing; reinstall requirements to enable browser push"
        )
        return
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=subscription.payload,
                data=json.dumps(notification, ensure_ascii=False),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                db.delete(subscription)
                db.commit()
            logger.warning(
                "Web Push delivery failed for subscription %s (%s)",
                subscription.id,
                status_code,
            )
