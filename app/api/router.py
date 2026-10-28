import re

from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.domain import router as domain_router
from app.api.inhouse_router import router as inhouse_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(inhouse_router)


def _included_routes(router: APIRouter, prefix: str = ""):
    """Yield routes from nested routers across FastAPI router implementations."""
    for route in router.routes:
        path = getattr(route, "path", None)
        if path is not None:
            yield route, f"{prefix}{path}"
            continue
        nested_router = getattr(route, "original_router", None)
        include_context = getattr(route, "include_context", None)
        if nested_router is not None:
            nested_prefix = getattr(include_context, "prefix", "") or ""
            yield from _included_routes(nested_router, f"{prefix}{nested_prefix}")


# Retain legacy routes not superseded by the frontend's in-house API contract.
inhouse_keys = {
    (method, re.sub(r"\{[^}]+\}", "{}", path))
    for route, path in _included_routes(inhouse_router)
    for method in getattr(route, "methods", set())
}
legacy_router = APIRouter()
legacy_router.routes.extend(
    route
    for route, path in _included_routes(domain_router)
    if not any(
        (method, re.sub(r"\{[^}]+\}", "{}", path)) in inhouse_keys
        for method in getattr(route, "methods", set())
    )
)
api_router.include_router(legacy_router)
