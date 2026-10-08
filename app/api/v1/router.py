from fastapi import APIRouter

from app.api.v1.routes import api_keys, auth, children, console_aml, console_api_keys, console_applicants, console_audit, console_auth, console_clients, console_metrics, console_reviews, console_settings, console_team, console_verifications, due_diligence_forms, session_steps, verification_sessions, documents, face, health, verifications

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router)
api_router.include_router(console_auth.router)
api_router.include_router(console_reviews.router)
api_router.include_router(console_verifications.router)
api_router.include_router(console_clients.router)
api_router.include_router(console_api_keys.router)
api_router.include_router(console_aml.router)
api_router.include_router(console_audit.router)
api_router.include_router(console_metrics.router)
api_router.include_router(console_settings.router)
api_router.include_router(console_team.router)
api_router.include_router(console_applicants.router)
api_router.include_router(due_diligence_forms.router)
api_router.include_router(api_keys.router)
api_router.include_router(verification_sessions.router)
api_router.include_router(session_steps.router)
api_router.include_router(children.router)
api_router.include_router(documents.router, tags=["documents"])
api_router.include_router(face.router, tags=["face"])
api_router.include_router(verifications.router, tags=["verifications"])

from app.api.v1.routes import screening
api_router.include_router(screening.router)
