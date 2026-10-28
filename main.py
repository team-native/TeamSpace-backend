from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.db.session import Base, SessionLocal, engine
from app.models.bank import BankImport, BankSyncRun  # noqa: F401
from app.models.resource import Resource  # noqa: F401
from app.models.user import User  # noqa: F401
from app.services.anonymity import anonymize_legacy_content


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        anonymize_legacy_content(db)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/api-docs",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_base_url, "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/health", tags=["health"])
def health_check() -> dict:
    return {
        "status": "healthy",
        "message": "The application is running smoothly.",
        "code": 200,
    }
