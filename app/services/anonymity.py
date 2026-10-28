import hashlib
import hmac
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.resource import Resource
from app.models.user import User

ANONYMOUS_LABEL = "익명"
ANONYMOUS_RESOURCE_TYPES = {
    "post",
    "comment",
    "inquiry",
    "proposal",
    "proposal_comment",
}
MIGRATION_RESOURCE_TYPES = ANONYMOUS_RESOURCE_TYPES | {"upload"}
PRIVATE_AUTHOR_FIELDS = {
    "authorId",
    "author_id",
    "authorEmail",
    "authorName",
    "ownerId",
    "owner_id",
    "userId",
    "user_id",
    "userEmail",
    "userName",
}


def anonymous_owner_key(user_id: int) -> str:
    """Return a stable opaque owner key without storing a user's database ID."""
    secret = settings.anonymous_author_secret or settings.jwt_secret_key
    digest = hmac.new(
        secret.encode("utf-8"),
        f"teamspace:anonymous-owner:v1:{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v1:{digest}"


def user_owns_anonymous_key(user_id: int, stored_key: str | None) -> bool:
    return bool(
        stored_key
        and hmac.compare_digest(str(stored_key), anonymous_owner_key(user_id))
    )


def owner_filter(user: User):
    return or_(
        Resource.owner_id == user.id,
        Resource.payload["anonymousOwnerKey"].as_string()
        == anonymous_owner_key(user.id),
    )


def is_record_owner(record: Resource, user: User) -> bool:
    if record.owner_id == user.id:
        return True
    return user_owns_anonymous_key(
        user.id, (record.payload or {}).get("anonymousOwnerKey")
    )


def anonymize_payload(payload: dict, user: User) -> dict:
    data = {
        key: value for key, value in payload.items() if key not in PRIVATE_AUTHOR_FIELDS
    }
    data["author"] = ANONYMOUS_LABEL
    data["anonymousOwnerKey"] = anonymous_owner_key(user.id)
    return data


def public_content_payload(payload: dict) -> dict:
    """Strip internal ownership keys before returning content to any client."""
    return {
        key: value
        for key, value in payload.items()
        if key not in PRIVATE_AUTHOR_FIELDS and key != "anonymousOwnerKey"
    }


def anonymize_legacy_content(db: Session) -> int:
    """Remove direct user foreign keys from legacy anonymous content once at startup."""
    rows: Iterable[Resource] = db.scalars(
        select(Resource).where(Resource.resource_type.in_(MIGRATION_RESOURCE_TYPES))
    )
    records = list(rows)
    by_id = {record.id: record for record in records}
    owner_ids = {record.id: record.owner_id for record in records}
    changed = 0

    posts = [
        record for record in records if record.resource_type in {"post", "proposal"}
    ]
    remaining = [
        record
        for record in records
        if record.resource_type in {"comment", "inquiry", "proposal_comment"}
    ]
    for record in posts:
        data = dict(record.payload or {})
        anonymous = bool(data.get("anonymous") or data.get("isAnonymous"))
        if not anonymous:
            continue
        original_data = dict(record.payload or {})
        original_owner_id = record.owner_id
        data["anonymous"] = True
        if record.owner_id is not None:
            data["anonymousOwnerKey"] = anonymous_owner_key(record.owner_id)
            record.owner_id = None
        data = {
            key: value
            for key, value in data.items()
            if key not in PRIVATE_AUTHOR_FIELDS
        }
        data["author"] = ANONYMOUS_LABEL
        data.pop("isAnonymous", None)
        if record.resource_type == "proposal" and isinstance(
            data.get("comments"), list
        ):
            comments = []
            for comment in data["comments"]:
                if not isinstance(comment, dict):
                    continue
                comment_data = dict(comment)
                if comment_data.get("anonymous") or comment_data.get("isAnonymous"):
                    comment_data = {
                        key: value
                        for key, value in comment_data.items()
                        if key not in PRIVATE_AUTHOR_FIELDS
                    }
                    comment_data["anonymous"] = True
                    comment_data["author"] = ANONYMOUS_LABEL
                    comment_data["isProposalAuthor"] = bool(
                        comment_data.get("isProposalAuthor")
                    )
                comments.append(comment_data)
            data["comments"] = comments
        record.payload = data
        if data != original_data or record.owner_id != original_owner_id:
            changed += 1

    for record in remaining:
        data = dict(record.payload or {})
        original_data = dict(data)
        original_owner_id = record.owner_id
        owner_id = owner_ids.get(record.id)
        parent_post = None
        if record.resource_type in {"comment", "proposal_comment"}:
            parent_id_key = (
                "postId" if record.resource_type == "comment" else "proposalId"
            )
            try:
                parent_post = by_id.get(int(data.get(parent_id_key)))
            except (TypeError, ValueError):
                parent_post = None
            same_post_author = bool(
                parent_post is not None
                and (parent_post.payload or {}).get("anonymous")
                and owner_id is not None
                and (
                    owner_ids.get(parent_post.id) == owner_id
                    or hmac.compare_digest(
                        str((parent_post.payload or {}).get("anonymousOwnerKey", "")),
                        anonymous_owner_key(owner_id),
                    )
                )
            )
            if same_post_author:
                data["anonymous"] = True
                data[
                    "isProposalAuthor"
                    if record.resource_type == "proposal_comment"
                    else "isPostAuthor"
                ] = True
        anonymous = bool(data.get("anonymous") or data.get("isAnonymous"))
        if not anonymous:
            continue
        data["anonymous"] = True
        if owner_id is not None:
            data["anonymousOwnerKey"] = anonymous_owner_key(owner_id)
            record.owner_id = None
        data = {
            key: value
            for key, value in data.items()
            if key not in PRIVATE_AUTHOR_FIELDS
        }
        data["author"] = ANONYMOUS_LABEL
        if record.resource_type == "inquiry":
            replies = []
            for reply in data.get("replies", []):
                reply_data = dict(reply)
                if reply_data.get("who") == ANONYMOUS_LABEL:
                    reply_data.pop("whoId", None)
                    reply_data.pop("userId", None)
                replies.append(reply_data)
            if "replies" in data:
                data["replies"] = replies
        record.payload = data
        if data != original_data or record.owner_id != original_owner_id:
            changed += 1

    for post in posts:
        if post.resource_type != "post" or not (post.payload or {}).get("anonymous"):
            continue
        post_owner_id = owner_ids.get(post.id)
        owner_key = post.payload.get("anonymousOwnerKey")
        if post_owner_id is None or not owner_key:
            post_owner_key = owner_key
        else:
            post_owner_key = anonymous_owner_key(post_owner_id)
        for attachment in post.payload.get("attachments", []):
            attachment_id = (
                attachment.get("id") if isinstance(attachment, dict) else attachment
            )
            try:
                upload = by_id.get(int(attachment_id))
            except (TypeError, ValueError):
                upload = None
            upload_owner_id = owner_ids.get(upload.id) if upload is not None else None
            if (
                upload is not None
                and upload.resource_type == "upload"
                and upload_owner_id is not None
                and hmac.compare_digest(
                    anonymous_owner_key(upload_owner_id), str(post_owner_key or "")
                )
            ):
                upload_payload = {
                    **{
                        key: value
                        for key, value in (upload.payload or {}).items()
                        if key not in PRIVATE_AUTHOR_FIELDS
                    },
                    "anonymousOwnerKey": owner_key,
                }
                upload.owner_id = None
                upload.payload = upload_payload
                changed += 1

    if changed:
        db.commit()
    return changed


def resolve_anonymous_owner(db: Session, key: str | None) -> User | None:
    """Resolve an opaque key for server-side notifications without persisting IDs."""
    if not key:
        return None
    for user in db.scalars(select(User)):
        if hmac.compare_digest(key, anonymous_owner_key(user.id)):
            return user
    return None
