from fastapi import APIRouter

from app.api.account import router as account_router
from app.api.admin import router as admin_router
from app.api.community import router as community_router
from app.api.documents import router as documents_router
from app.api.finance import router as finance_router
from app.api.search import router as search_router
from app.api.space import router as space_router
from app.api.workflows import router as workflows_router

router = APIRouter()
router.include_router(account_router)
router.include_router(workflows_router)
router.include_router(finance_router)
router.include_router(documents_router)
router.include_router(space_router)
router.include_router(search_router)
router.include_router(community_router)
router.include_router(admin_router)
