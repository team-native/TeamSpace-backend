from datetime import date, datetime
from typing import Any

from fastapi import HTTPException

SCOPE_ALIASES = {
    "팀": "팀",
    "team": "팀",
    "프로젝트": "프로젝트",
    "project": "프로젝트",
    "개인": "개인",
    "personal": "개인",
}


def normalize_event_payload(
    payload: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize event fields while retaining unrelated app-specific metadata."""
    previous = previous or {}
    data = {**previous, **payload}

    event_date = data.get("date")
    if not event_date:
        raise HTTPException(422, "일정 날짜를 입력해주세요.")
    try:
        normalized_date = date.fromisoformat(str(event_date)).isoformat()
        if normalized_date != str(event_date):
            raise ValueError
        data["date"] = normalized_date
    except ValueError:
        raise HTTPException(422, "date는 YYYY-MM-DD 형식이어야 합니다.") from None

    start = payload.get(
        "start", payload.get("time", data.get("start", data.get("time", "")))
    )
    end = data.get("end", "")
    for field, value in (("start", start), ("end", end)):
        if value in (None, ""):
            continue
        try:
            parsed_time = datetime.strptime(str(value), "%H:%M")
            if parsed_time.strftime("%H:%M") != str(value):
                raise ValueError
        except ValueError:
            raise HTTPException(422, f"{field}는 HH:mm 형식이어야 합니다.") from None
    data["start"] = str(start or "")
    data["end"] = str(end or "")
    # Keep the old key during migration for clients that still read it.
    data["time"] = data["start"]

    raw_scope = str(
        payload.get(
            "scope", payload.get("kind", data.get("scope", data.get("kind", "팀")))
        )
    )
    if raw_scope not in SCOPE_ALIASES:
        raise HTTPException(422, "scope는 팀, 프로젝트, 개인 중 하나여야 합니다.")
    data["scope"] = SCOPE_ALIASES[raw_scope]
    data.setdefault("place", "")
    data.setdefault("host", "")
    data.setdefault("agenda", "")
    data["audience"] = data.get("audience", data.get("target", "전체"))
    data["target"] = data.get("target", data["audience"])
    data["relatedDocs"] = data.get(
        "relatedDocs", data.get("documentIds", data.get("docs", []))
    )
    rsvp = payload.get(
        "rsvp",
        payload.get("rsvpEnabled", data.get("rsvp", data.get("rsvpEnabled", False))),
    )
    if not isinstance(rsvp, bool):
        raise HTTPException(422, "rsvp는 true 또는 false여야 합니다.")
    data["rsvp"] = rsvp
    data.pop("rsvpEnabled", None)
    return data


def event_response(
    record_id: int,
    payload: dict[str, Any],
    user_id: int,
) -> dict[str, Any]:
    data = dict(payload)
    responses = data.get("rsvpByUser", {})
    own_response = responses.get(str(user_id)) if isinstance(responses, dict) else None
    result = {
        "id": str(record_id),
        **{key: value for key, value in data.items() if key != "rsvpByUser"},
    }
    result["start"] = data.get("start", data.get("time", ""))
    result["end"] = data.get("end", "")
    result["scope"] = SCOPE_ALIASES.get(
        str(data.get("scope", data.get("kind", "팀"))),
        data.get("scope", data.get("kind", "팀")),
    )
    result["place"] = data.get("place", "")
    result["host"] = data.get("host", "")
    result["agenda"] = data.get("agenda", "")
    result["audience"] = data.get("audience", data.get("target", "전체"))
    result["target"] = data.get("target", result["audience"])
    result["relatedDocs"] = data.get(
        "relatedDocs", data.get("documentIds", data.get("docs", []))
    )
    result["history"] = data.get("history", [])
    result["rsvp"] = bool(data.get("rsvp", data.get("rsvpEnabled", False)))
    result["myRsvp"] = (
        own_response.get("rsvp") if isinstance(own_response, dict) else own_response
    )
    result["respondedAt"] = (
        own_response.get("respondedAt", own_response.get("at"))
        if isinstance(own_response, dict)
        else None
    )
    result["rsvpCount"] = (
        sum(
            isinstance(response, dict) and response.get("rsvp") == "참석"
            for response in responses.values()
        )
        if isinstance(responses, dict)
        else 0
    )
    return result
