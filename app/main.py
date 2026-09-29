from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.pages import router as pages_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.security import check_configuration


def create_app() -> FastAPI:
    check_configuration()
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url=None,
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
