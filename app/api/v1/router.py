from fastapi import APIRouter

from app.api.v1.routes import documents, face, health, verifications

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(documents.router, tags=["documents"])
api_router.include_router(face.router, tags=["face"])
api_router.include_router(verifications.router, tags=["verifications"])
