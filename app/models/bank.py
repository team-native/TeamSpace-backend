from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class BankImport(Base):
    """A bank transaction staged for review before it affects finance records."""

    __tablename__ = "bank_imports"
    __table_args__ = (
        UniqueConstraint("transaction_key", name="uq_bank_imports_transaction_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_transaction_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    transaction_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    counterparty: Mapped[str] = mapped_column(String(255), default="")
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    balance_after: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matched_project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    matched_contract_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(80), default="")
    committed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BankSyncRun(Base):
    """Audit record for each attempted bank synchronization."""

    __tablename__ = "bank_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetched_count: Mapped[int] = mapped_column(default=0)
    imported_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    triggered_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
