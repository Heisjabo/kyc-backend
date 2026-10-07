from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.pages import router as pages_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.security import check_configuration
from app.services import network


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The IP databases and lists are fetched in the background, so the service
    # answers at once and the first sessions are looked up as soon as they land.
    network.refresh_in_background()
    yield


def create_app() -> FastAPI:
    check_configuration()
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=600,
    )

    app.include_router(api_router, prefix="/v1")
    app.include_router(pages_router)

    return app


app = create_app()
