from typing import Any, Protocol

from app.core.config import Settings


class BankProviderUnavailable(RuntimeError):
    """Raised when no supported bank or open-banking adapter is configured."""


class BankProvider(Protocol):
    def fetch_transactions(
        self, *, since: str | None = None
    ) -> list[dict[str, Any]]: ...


def get_bank_provider(settings: Settings) -> BankProvider | None:
    """Return the vendor adapter once the operating bank/service is selected.

    The requirements intentionally leave the bank and its API undecided. Do not
    guess an endpoint or fabricate transactions: add the selected vendor's
    adapter here when its authentication and response contract are available.
    """
    return None


def fetch_transactions(
    settings: Settings, *, since: str | None = None
) -> list[dict[str, Any]]:
    provider = get_bank_provider(settings)
    if provider is None:
        raise BankProviderUnavailable(
            "은행 또는 거래내역 조회 서비스가 아직 선택되지 않아 자동 불러오기를 사용할 수 없습니다."
        )
    return provider.fetch_transactions(since=since)
