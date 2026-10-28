import json
import logging
import re
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import envelope, notify_user, user_can_finance
from app.core.config import settings
from app.db.session import get_db
from app.models.bank import BankImport, BankSyncRun
from app.models.resource import Resource
from app.models.user import User
from app.services.bank import BankProviderUnavailable, fetch_transactions
from app.services.money import parse_money

router = APIRouter()
KST = ZoneInfo("Asia/Seoul")
FINANCE_ADMIN_ROLES = {"Admin", "FA"}
logger = logging.getLogger(__name__)


def _require_finance_admin(user: User) -> None:
    if not set(user.roles or []).intersection(FINANCE_ADMIN_ROLES):
        raise HTTPException(403, "은행 거래를 관리할 권한이 없습니다.")


def _money(value: Any) -> int:
    try:
        return parse_money(value)
    except ValueError as exc:
        raise HTTPException(422, "금액 형식이 올바르지 않습니다.") from exc


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=KST)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("거래 일시가 없습니다.")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(timezone.utc)


def _normalize_transaction(
    raw: dict[str, Any], account_fingerprint: str
) -> dict[str, Any]:
    transaction_at = _aware_datetime(
        raw.get("transactionAt") or raw.get("date") or raw.get("datetime")
    )
    amount = abs(_money(raw.get("amount")))
    if amount <= 0:
        raise ValueError("거래 금액은 0보다 커야 합니다.")
    direction_value = str(raw.get("direction") or raw.get("type") or "").strip().lower()
    if direction_value in {"입금", "deposit", "credit", "in", "입", "income"}:
        direction = "입금"
    elif direction_value in {"출금", "withdrawal", "debit", "out", "출", "expense"}:
        direction = "출금"
    else:
        # A signed amount is a safe fallback for providers that omit direction.
        raw_amount = _money(raw.get("amount"))
        if raw_amount == 0:
            raise ValueError("거래 방향을 확인할 수 없습니다.")
        direction = "출금" if raw_amount < 0 else "입금"
    counterparty = str(
        raw.get("counterparty") or raw.get("counterpartyName") or raw.get("payer") or ""
    ).strip()
    provider_id = str(raw.get("transactionId") or raw.get("id") or "").strip() or None
    balance = raw.get("balanceAfter") if "balanceAfter" in raw else raw.get("balance")
    balance_after = None if balance is None else _money(balance)
    identity = {
        "account": account_fingerprint,
        "providerTransactionId": provider_id,
        "date": transaction_at.isoformat(),
        "counterparty": re.sub(r"\s+", "", counterparty).casefold(),
        "amount": amount,
        "direction": direction,
        "balanceAfter": balance_after,
    }
    key = sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "transaction_key": key,
        "provider_transaction_id": provider_id,
        "transaction_at": transaction_at,
        "counterparty": counterparty[:255],
        "amount": amount,
        "direction": direction,
        "balance_after": balance_after,
    }


def _contract_label(contract: Resource) -> str:
    return str(
        contract.payload.get("project") or contract.payload.get("projectId") or ""
    ).strip()


def _contract_remaining(db: Session, contract: Resource) -> int:
    payload = contract.payload or {}
    total = _money(
        payload.get("totalAmount", payload.get("amount", payload.get("total")))
    )
    collected = _contract_collected(db, contract)
    return max(total - collected, 0)


def _contract_collected(db: Session, contract: Resource) -> int:
    payload = contract.payload or {}
    if (
        payload.get("collectedAmount") is not None
        or payload.get("collected") is not None
    ):
        return _money(payload.get("collectedAmount", payload.get("collected")))
    project = _contract_label(contract)
    collected = 0
    for receipt in db.scalars(
        select(Resource).where(Resource.resource_type == "finance_receipt")
    ):
        receipt_project = str(
            receipt.payload.get("projectId") or receipt.payload.get("project") or ""
        ).strip()
        if receipt.payload.get("status") == "입금 완료" and receipt_project == project:
            collected += _money(receipt.payload.get("amount"))
    return collected


def _find_matching_contract(
    db: Session, counterparty: str, amount: int
) -> Resource | None:
    normalized = re.sub(r"\s+", "", counterparty).casefold()
    if not normalized:
        return None
    candidates = []
    for contract in db.scalars(
        select(Resource).where(Resource.resource_type == "finance_contract")
    ):
        client = str(
            contract.payload.get("client") or contract.payload.get("customer") or ""
        )
        if (
            re.sub(r"\s+", "", client).casefold() == normalized
            and _contract_remaining(db, contract) >= amount
        ):
            candidates.append(contract)
    return candidates[0] if len(candidates) == 1 else None


def _bank_account_fingerprint() -> str:
    return sha256(
        (settings.bank_account_number or "unconfigured").encode("utf-8")
    ).hexdigest()


def _consecutive_failures(db: Session) -> int:
    failures = 0
    for run in db.scalars(
        select(BankSyncRun).order_by(BankSyncRun.id.desc()).limit(100)
    ):
        if run.status == "success":
            break
        if run.status == "failure":
            failures += 1
    return failures


def last_successful_sync(db: Session) -> datetime | None:
    return db.scalar(
        select(BankSyncRun.finished_at)
        .where(BankSyncRun.status == "success", BankSyncRun.finished_at.is_not(None))
        .order_by(BankSyncRun.id.desc())
        .limit(1)
    )


def _notify_finance_admins(db: Session, title: str, href: str) -> None:
    for admin in db.scalars(select(User)):
        if user_can_finance(admin):
            notify_user(db, admin.id, "재정", title, href, action=True)


class BankImportReview(BaseModel):
    matched_project: str | None = Field(default=None, alias="matchedProject")
    confirm: bool = False
    exclude: bool = False

    model_config = ConfigDict(populate_by_name=True)


class BankImportCommit(BaseModel):
    row_ids: list[str] | None = Field(default=None, alias="rowIds", max_length=500)

    model_config = ConfigDict(populate_by_name=True)


@router.get("/finance/bank-accounts", summary="은행 계좌 연동 상태")
def get_bank_account(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if not user_can_finance(user):
        raise HTTPException(403, "재정 정보 열람 권한이 없습니다.")
    now_kst = datetime.now(KST)
    month_start = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    month_counts = dict(
        db.execute(
            select(BankImport.direction, func.count(BankImport.id))
            .where(
                BankImport.transaction_at >= month_start.astimezone(timezone.utc),
                BankImport.transaction_at < month_end.astimezone(timezone.utc),
            )
            .group_by(BankImport.direction)
        ).all()
    )
    runs = list(
        db.scalars(select(BankSyncRun).order_by(BankSyncRun.id.desc()).limit(5))
    )
    last_synced = last_successful_sync(db)
    configured = bool(
        settings.bank_provider
        and settings.bank_account_number
        and settings.bank_account_alias
    )
    account_number = settings.bank_account_number or ""
    masked = (
        ("*" * max(len(account_number) - 4, 0) + account_number[-4:])
        if account_number
        else None
    )
    return envelope(
        {
            "connected": False,  # No bank vendor adapter is safe to claim until the provider is selected.
            "configured": configured,
            "connectionStatus": "provider_pending" if configured else "not_configured",
            "provider": settings.bank_provider,
            "accountNumber": masked,
            "alias": settings.bank_account_alias,
            "lastSyncedAt": last_synced.isoformat() if last_synced else None,
            "thisMonth": {
                "inboundCount": int(month_counts.get("입금", 0)),
                "outboundCount": int(month_counts.get("출금", 0)),
            },
            "consecutiveFailures": _consecutive_failures(db),
            "recentFailures": [
                {
                    "at": r.finished_at.isoformat() if r.finished_at else None,
                    "reason": r.error_message,
                }
                for r in runs
                if r.status == "failure"
            ],
        }
    )


@router.post("/finance/bank-syncs", summary="은행 거래 동기화 실행")
def sync_bank_account(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _require_finance_admin(user)
    now = datetime.now(timezone.utc)
    run = BankSyncRun(status="running", started_at=now, triggered_by=user.id)
    db.add(run)
    db.flush()
    try:
        last_sync = last_successful_sync(db)
        raw_transactions = fetch_transactions(
            settings, since=last_sync.isoformat() if last_sync else None
        )
        if not isinstance(raw_transactions, list):
            raise ValueError("은행 연동 응답 형식이 올바르지 않습니다.")
        run.fetched_count = len(raw_transactions)
        imported = 0
        for raw in raw_transactions:
            normalized = _normalize_transaction(raw, _bank_account_fingerprint())
            existing = db.scalar(
                select(BankImport.id).where(
                    BankImport.transaction_key == normalized["transaction_key"]
                )
            )
            if existing is not None:
                continue
            matched = None
            status_value = "확인 필요"
            if normalized["direction"] == "입금":
                matched = _find_matching_contract(
                    db, normalized["counterparty"], normalized["amount"]
                )
                if matched:
                    status_value = "자동 매칭"
            db.add(
                BankImport(
                    **normalized,
                    matched_project=_contract_label(matched) if matched else None,
                    matched_contract_id=matched.id if matched else None,
                    status=status_value,
                    confirmed=False,
                    source=settings.bank_provider or "",
                )
            )
            imported += 1
        run.status = "success"
        run.imported_count = imported
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = None
        db.commit()
        return envelope(
            {
                "success": True,
                "fetchedCount": run.fetched_count,
                "importedCount": imported,
                "lastSyncedAt": run.finished_at.isoformat(),
            }
        )
    except BankProviderUnavailable as exc:
        db.rollback()
        run.status = "failure"
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = str(exc)[:500]
        db.add(run)
        db.commit()
        if _consecutive_failures(db) == 3:
            _notify_finance_admins(
                db, "은행 거래 불러오기가 3회 연속 실패했어요.", "/design/admin"
            )
        raise HTTPException(
            503,
            detail={
                "message": run.error_message,
                "lastSuccessfulSyncAt": (
                    last_successful_sync(db).isoformat()
                    if last_successful_sync(db)
                    else None
                ),
            },
        ) from None
    except Exception as exc:
        db.rollback()
        run.status = "failure"
        run.finished_at = datetime.now(timezone.utc)
        # Keep provider error text for operations, but never include request credentials.
        logger.exception("Bank transaction sync failed")
        run.error_message = f"은행 거래 조회 중 오류가 발생했습니다 ({type(exc).__name__}). 서버 로그를 확인해주세요."
        db.add(run)
        db.commit()
        if _consecutive_failures(db) == 3:
            _notify_finance_admins(
                db, "은행 거래 불러오기가 3회 연속 실패했어요.", "/design/admin"
            )
        raise HTTPException(
            502,
            detail={
                "message": "은행 거래를 불러오지 못했습니다.",
                "reason": run.error_message,
                "lastSuccessfulSyncAt": (
                    last_successful_sync(db).isoformat()
                    if last_successful_sync(db)
                    else None
                ),
            },
        ) from None


@router.get("/finance/bank-imports", summary="불러온 은행 거래 목록")
def get_bank_imports(
    status_filter: str | None = Query(None, alias="status"),
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not user_can_finance(user):
        raise HTTPException(403, "재정 정보 열람 권한이 없습니다.")
    stmt = select(BankImport).order_by(BankImport.id.desc())
    if cursor:
        try:
            stmt = stmt.where(BankImport.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    if status_filter:
        stmt = stmt.where(BankImport.status == status_filter)
    records = list(db.scalars(stmt.limit(limit + 1)))
    page = records[:limit]
    items = [
        {
            "rowId": str(r.id),
            "date": r.transaction_at.astimezone(KST).isoformat(),
            "counterparty": r.counterparty,
            "amount": r.amount,
            "direction": r.direction,
            "matchedProject": r.matched_project,
            "status": r.status,
            "confirmed": r.confirmed,
            "balanceAfter": r.balance_after,
            "committedAt": r.committed_at.isoformat() if r.committed_at else None,
            "committedBy": (
                db.get(User, r.committed_by).name
                if r.committed_by and db.get(User, r.committed_by)
                else None
            ),
        }
        for r in page
    ]
    next_cursor = str(page[-1].id) if len(records) > limit and page else None
    return envelope({"items": items, "nextCursor": next_cursor})


@router.patch("/finance/bank-imports/{row_id}", summary="은행 거래 연결 확인 또는 제외")
def review_bank_import(
    row_id: int,
    payload: BankImportReview,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_finance_admin(user)
    record = db.get(BankImport, row_id)
    if record is None:
        raise HTTPException(404, "은행 거래를 찾을 수 없습니다.")
    if record.status == "이미 반영됨":
        raise HTTPException(409, "이미 재정에 반영된 거래입니다.")
    if payload.exclude:
        record.status = "제외"
        record.confirmed = False
        db.commit()
        return envelope(
            {"rowId": str(record.id), "status": record.status},
            "거래를 반영 대상에서 제외했습니다.",
        )
    if record.direction == "입금":
        contract = (
            db.get(Resource, record.matched_contract_id)
            if not payload.matched_project and record.matched_contract_id
            else None
        )
        if payload.matched_project:
            contract = db.scalar(
                select(Resource).where(
                    Resource.resource_type == "finance_contract",
                    (
                        Resource.payload["projectId"].as_string()
                        == payload.matched_project
                    )
                    | (
                        Resource.payload["project"].as_string()
                        == payload.matched_project
                    ),
                )
            )
        if contract is None:
            raise HTTPException(404, "연결할 수주 계약을 찾을 수 없습니다.")
        if contract.resource_type != "finance_contract":
            raise HTTPException(404, "연결할 수주 계약을 찾을 수 없습니다.")
        if _contract_remaining(db, contract) < record.amount:
            raise HTTPException(409, "거래 금액이 계약의 미수 잔액보다 큽니다.")
        record.matched_contract_id = contract.id
        record.matched_project = _contract_label(contract)
        record.confirmed = payload.confirm
        record.status = "확인 완료" if payload.confirm else "자동 매칭"
    else:
        if payload.matched_project:
            raise HTTPException(422, "출금 거래는 수주 계약에 연결할 수 없습니다.")
        record.confirmed = payload.confirm
        record.status = "확인 완료" if payload.confirm else "확인 필요"
    db.commit()
    return envelope(
        {
            "rowId": str(record.id),
            "matchedProject": record.matched_project,
            "confirmed": record.confirmed,
            "status": record.status,
        },
        "은행 거래 확인 상태를 저장했습니다.",
    )


def _default_pools() -> list[dict[str, Any]]:
    return [
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
    ]


def _allocate_to_pools(db: Session, amount: int) -> dict[str, int]:
    settings_row = db.scalar(
        select(Resource).where(Resource.resource_type == "finance_settings")
    )
    finance = (
        dict(settings_row.payload)
        if settings_row
        else {"pools": _default_pools(), "flow": [], "notices": []}
    )
    pools = [dict(pool) for pool in finance.get("pools") or _default_pools()]
    positive = [pool for pool in pools if _money(pool.get("ratio")) > 0]
    ratio_total = sum(_money(pool.get("ratio")) for pool in positive)
    if not positive or ratio_total <= 0:
        pools = _default_pools()
        positive = pools
        ratio_total = 100
    allocation: dict[str, int] = {}
    remaining = amount
    for index, pool in enumerate(positive):
        share = (
            remaining
            if index == len(positive) - 1
            else amount * _money(pool.get("ratio")) // ratio_total
        )
        pool["amount"] = _money(pool.get("amount")) + share
        allocation[str(pool.get("id", index))] = share
        remaining -= share
    finance["pools"] = pools
    if settings_row:
        settings_row.payload = finance
    else:
        db.add(
            Resource(resource_type="finance_settings", owner_id=None, payload=finance)
        )
        # SessionLocal disables autoflush; flush so a second receipt in this
        # same commit batch reuses the settings row instead of creating one more.
        db.flush()
    return allocation


@router.post("/finance/bank-import-commits", summary="확인된 은행 거래 반영")
def commit_bank_imports(
    payload: BankImportCommit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_finance_admin(user)
    selected_ids: list[int] | None = None
    if payload.row_ids is not None:
        try:
            selected_ids = [int(value) for value in payload.row_ids]
        except ValueError:
            raise HTTPException(422, "rowIds 값은 거래 ID여야 합니다.") from None
    stmt = select(BankImport).where(
        BankImport.confirmed.is_(True), BankImport.status == "확인 완료"
    )
    if selected_ids is not None:
        stmt = stmt.where(BankImport.id.in_(selected_ids))
    records = list(db.scalars(stmt.order_by(BankImport.id).with_for_update()))
    if selected_ids is not None and len(records) != len(set(selected_ids)):
        raise HTTPException(
            409, "선택한 거래 중 확인되지 않았거나 이미 반영된 항목이 있습니다."
        )
    now = datetime.now(timezone.utc)
    committed: list[BankImport] = []
    imported_receipts = 0
    imported_expenses = 0
    for record in records:
        if record.direction == "입금":
            contract = (
                db.get(Resource, record.matched_contract_id)
                if record.matched_contract_id
                else None
            )
            if contract is None or contract.resource_type != "finance_contract":
                raise HTTPException(
                    409, f"거래 {record.id}에 연결된 수주 계약을 확인할 수 없습니다."
                )
            if _contract_remaining(db, contract) < record.amount:
                raise HTTPException(
                    409, f"거래 {record.id} 금액이 남은 청구액을 초과합니다."
                )
            project = _contract_label(contract)
            client = str(
                contract.payload.get("client")
                or contract.payload.get("customer")
                or record.counterparty
            )
            allocation = _allocate_to_pools(db, record.amount)
            db.add(
                Resource(
                    resource_type="finance_receipt",
                    owner_id=user.id,
                    payload={
                        "client": client,
                        "project": project,
                        "projectId": str(contract.payload.get("projectId") or project),
                        "amount": record.amount,
                        "status": "입금 완료",
                        "date": record.transaction_at.astimezone(KST).isoformat(),
                        "bankImportId": str(record.id),
                        "poolAllocation": allocation,
                    },
                )
            )
            contract.payload = {
                **contract.payload,
                "collectedAmount": _contract_collected(db, contract) + record.amount,
            }
            imported_receipts += 1
        else:
            db.add(
                Resource(
                    resource_type="finance_bank_expense",
                    owner_id=user.id,
                    payload={
                        "amount": record.amount,
                        "date": record.transaction_at.astimezone(KST).isoformat(),
                        "counterparty": record.counterparty,
                        "bankImportId": str(record.id),
                        "status": "반영 완료",
                    },
                )
            )
            imported_expenses += 1
        record.status = "이미 반영됨"
        record.confirmed = False
        record.committed_by = user.id
        record.committed_at = now
        committed.append(record)
    if committed:
        db.add(
            Resource(
                resource_type="finance_bank_commit",
                owner_id=user.id,
                payload={
                    "committedAt": now.isoformat(),
                    "committedBy": user.name,
                    "committedById": str(user.id),
                    "rowCount": len(committed),
                    "receiptCount": imported_receipts,
                    "expenseCount": imported_expenses,
                    "rowIds": [str(row.id) for row in committed],
                },
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "이미 반영된 거래가 포함되어 있습니다. 거래 목록을 새로고침해주세요."
        ) from None
    if committed:
        _notify_finance_admins(
            db,
            f"은행 거래 {len(committed)}건이 재정에 반영됐어요.",
            "/inhouse/finance/receipts",
        )
    return envelope(
        {
            "committedCount": len(committed),
            "receiptCount": imported_receipts,
            "expenseCount": imported_expenses,
            "committedAt": now.isoformat(),
            "rowIds": [str(row.id) for row in committed],
        },
        "확인한 은행 거래를 재정에 반영했습니다.",
    )


@router.get("/finance/bank-import-commits", summary="은행 거래 반영 이력")
def get_bank_commit_history(
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not user_can_finance(user):
        raise HTTPException(403, "재정 정보 열람 권한이 없습니다.")
    stmt = (
        select(Resource)
        .where(Resource.resource_type == "finance_bank_commit")
        .order_by(Resource.id.desc())
    )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    rows = list(db.scalars(stmt.limit(limit + 1)))
    page = rows[:limit]
    return envelope(
        {
            "items": [{"id": str(row.id), **row.payload} for row in page],
            "nextCursor": str(page[-1].id) if len(rows) > limit and page else None,
        }
    )
