from fastapi import APIRouter

from app.api.v1.routes import api_keys, auth, documents, face, health, verifications

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(api_keys.router)
api_router.include_router(documents.router, tags=["documents"])
api_router.include_router(face.router, tags=["face"])
api_router.include_router(verifications.router, tags=["verifications"])
