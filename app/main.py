import utils.transport
import utils.session
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.pages import router as pages_router
from app.api.security_headers import SecurityHeadersMiddleware
from app.api.request_audit import RequestAuditMiddleware, flush_bursts, flush_bursts_forever
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.security import check_configuration
from app.services import network


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The IP databases and lists are fetched in the background, so the service
    # answers at once and the first sessions are looked up as soon as they land.
    network.refresh_in_background()
    # Grouped requests (camera frames, health checks) are written in bursts.
    flusher = asyncio.create_task(flush_bursts_forever())
    yield
    flusher.cancel()
    flush_bursts(everything=True)


def docs_enabled() -> bool:
    if settings.ENABLE_DOCS is None:
        return settings.ENVIRONMENT == "development"
    return settings.ENABLE_DOCS


def create_app() -> FastAPI:
    check_configuration()
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs" if docs_enabled() else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # Added before CORS so it sits inside it: preflight requests, which CORS
    # answers itself, are not recorded.
    app.add_middleware(RequestAuditMiddleware)

    origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=600,
    )

    # Outermost, so error responses and CORS preflights carry the headers too.
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(api_router, prefix="/v1")
    app.include_router(pages_router)

    return app


app = create_app()
