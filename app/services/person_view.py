"""What the person holding a verification link is sent back.

The reasons stay, so the person can see what to fix. What does not go to them is the
data we read off their documents, the individual checks and the scores: the client and
the console still get all of that from the stored record.
"""

from __future__ import annotations

from app.schemas.address import AddressCheckResponse, AddressExtracted
from app.schemas.document import DocumentCheckResponse
from app.schemas.face import ChallengeProgress, FaceMatchResult, FaceVerificationResponse, LivenessResponse


def document(result: DocumentCheckResponse) -> DocumentCheckResponse:
    return result.model_copy(update={"extracted": None, "checks": []})


def address(result: AddressCheckResponse) -> AddressCheckResponse:
    return result.model_copy(update={
        "extracted": AddressExtracted(document_type=result.extracted.document_type,
                                  full_address=result.extracted.full_address),
        "checks": [],
    })


def face(result: FaceVerificationResponse) -> FaceVerificationResponse:
    liveness = LivenessResponse(
        decision=result.liveness.decision, reasons=result.liveness.reasons, warnings=[],
        challenge=ChallengeProgress(type="", required=[], completed=[], missing=[]),
        baseline_yaw=0.0, baseline_pitch=0.0, frames_received=0, frames_with_face=0,
        poses=[], capture=None, checks=[], processing_ms=0,
    )
    match = FaceMatchResult(
        score=None, best_score=None, frames_compared=0, threshold=0.0,
        check=result.match.check.model_copy(update={"details": {}}),
    )
    return result.model_copy(update={"liveness": liveness, "match": match, "document": None})
