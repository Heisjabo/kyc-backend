"""Compare the face captured from the camera with the portrait on the document."""

import numpy as np

from app.engines.recognition import pass_threshold, similarity
from app.rules import face as rules
from app.schemas.document import CheckResult, CheckStatus
from app.schemas.face import FaceMatchResult


def match_faces(document_embedding: np.ndarray | None, selfie_embeddings: list[np.ndarray]) -> FaceMatchResult:
    """Score the capture against the document portrait.

    The score is the cosine similarity between two unit-length ArcFace vectors,
    and the pass line is MATCH_THRESHOLD, which carries in rules/face.py the
    measurements it was set from.

    One frame alone can flatter an impostor, and the mean over every frame lets a
    few poor ones sink a genuine person, so the score is the mean of the best
    MATCH_TOP_FRAMES. The verdict is pass or fail; every frame's score is
    reported so a person can see how it was reached.

    No confidence percentage is returned. The models give a similarity, not a
    probability, and turning one into the other honestly needs a calibration set
    we do not have. A number that looks like a probability and is not would be
    read as one.
    """
    passes_at = pass_threshold()

    if document_embedding is None:
        return FaceMatchResult(
            score=None,
            best_score=None,
            frames_compared=0,
            threshold=passes_at,
            check=CheckResult(name="face_match", status=CheckStatus.failed, reasons=["NO_FACE_ON_DOCUMENT"]),
        )
    if not selfie_embeddings:
        return FaceMatchResult(
            score=None,
            best_score=None,
            frames_compared=0,
            threshold=passes_at,
            check=CheckResult(name="face_match", status=CheckStatus.failed, reasons=["NO_FRONTAL_FRAME"]),
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
                "threshold_source": "measured on document-to-camera pairs, see rules/face.py MATCH_THRESHOLD",
            },
        ),
    )
