from fastapi import APIRouter

from app.api.v1.routes import api_keys, auth, children, console_auth, console_verifications, session_steps, verification_sessions, documents, face, health, verifications

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(console_auth.router)
api_router.include_router(console_verifications.router)
api_router.include_router(api_keys.router)
api_router.include_router(verification_sessions.router)
api_router.include_router(session_steps.router)
api_router.include_router(children.router)
api_router.include_router(documents.router, tags=["documents"])
api_router.include_router(face.router, tags=["face"])
api_router.include_router(verifications.router, tags=["verifications"])
