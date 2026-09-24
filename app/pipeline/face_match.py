"""Compare the face captured from the camera with the portrait on the document."""

import numpy as np

from app.engines.recognition import confidence, pass_threshold, similarity
from app.rules import face as rules
from app.schemas.document import CheckResult, CheckStatus
from app.schemas.face import FaceMatchResult


def match_faces(document_embedding: np.ndarray | None, selfie_embeddings: list[np.ndarray]) -> FaceMatchResult:
    """Score the capture against the document portrait.

    The pass line comes from DeepFace's tuned threshold for the configured model,
    so it moves with the model instead of being a number we invented.

    One frame alone can flatter an impostor, and the mean over every frame lets a
    few grainy ones sink a genuine person, so the score is the mean of the best
    MATCH_TOP_FRAMES. The verdict is pass or fail; every frame's score is
    reported so a person can see how it was reached.
    """
    # The published threshold is the floor, not the pass mark: see MATCH_PASS_MARGIN.
    line = pass_threshold()
    passes_at = line + rules.MATCH_PASS_MARGIN

    if document_embedding is None:
        return FaceMatchResult(
            score=None,
            confidence=None,
            best_score=None,
            frames_compared=0,
            threshold=passes_at,
            check=CheckResult(name="face_match", status=CheckStatus.failed, reasons=["NO_FACE_ON_DOCUMENT"]),
        )
    if not selfie_embeddings:
        return FaceMatchResult(
            score=None,
            confidence=None,
            best_score=None,
            frames_compared=0,
            threshold=passes_at,
            check=CheckResult(name="face_match", status=CheckStatus.failed, reasons=["NO_FACE_CAPTURED"]),
        )

    scores = [similarity(document_embedding, embedding) for embedding in selfie_embeddings]
    best = sorted(scores, reverse=True)[: rules.MATCH_TOP_FRAMES]
    score = float(np.mean(best))
    # Pass or fail, nothing in between.
    if score >= passes_at:
        status, reasons = CheckStatus.passed, []
    else:
        status, reasons = CheckStatus.failed, ["FACE_MISMATCH"]

    return FaceMatchResult(
        score=round(score, 4),
        confidence=confidence(score),
        best_score=round(max(scores), 4),
        frames_compared=len(scores),
        threshold=round(passes_at, 4),
        check=CheckResult(
            name="face_match",
            status=status,
            reasons=reasons,
            details={
                "scores": [round(value, 4) for value in scores],
                "scored_on": len(best),
                "mean_all_frames": round(float(np.mean(scores)), 4),
                "deepface_threshold": round(line, 4),
                "threshold_source": "deepface tuned threshold, plus a margin for card-to-selfie matching",
            },
        ),
    )
