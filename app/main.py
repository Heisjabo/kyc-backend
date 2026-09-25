from fastapi import FastAPI

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

    app.include_router(api_router, prefix="/v1")
    app.include_router(pages_router)

    return app


app = create_app()
