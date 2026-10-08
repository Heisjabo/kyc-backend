"""Server-side identity screening; records unavailable screening separately from a successful no-match."""
import os
import logging
import math
import unicodedata
from datetime import date, datetime, UTC
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, field_validator
from app.core.config import settings


logger = logging.getLogger("uvicorn.error")


class Identity(BaseModel):
    full_name: str = Field(min_length=2, max_length=250)
    date_of_birth: date | None = None
    nationality: str | None = Field(default=None, min_length=2, max_length=3)
    id_number: str | None = Field(default=None, max_length=100)

    @field_validator('full_name')
    @classmethod
    def normalize_name(cls, value):
        value = ' '.join(unicodedata.normalize('NFKC', value).split())
        if len(value) < 2:
            raise ValueError('A full name is required')
        return value

    def properties(self):
        properties = {'name': [self.full_name]}
        if self.date_of_birth:
            properties['birthDate'] = [self.date_of_birth.isoformat()]
        if self.nationality:
            # FollowTheMoney uses ISO alpha-2 country codes.
            import pycountry
            country = pycountry.countries.get(**{
                'alpha_2' if len(self.nationality) == 2 else 'alpha_3': self.nationality.upper()
            })
            if country is None:
                raise ValueError('Invalid nationality country code')
            properties['nationality'] = [country.alpha_2.lower()]
        if self.id_number:
            properties['idNumber'] = [self.id_number]
        return properties


class ScreeningResult(BaseModel):
    outcome: str
    action: str
    highest_score: float | None = None
    matches: list[dict] = Field(default_factory=list)
    dataset: str
    screened_at: str
    error: str | None = None


def screen(identity: Identity) -> ScreeningResult:
    dataset = os.environ.get('YENTE_SCREENING_DATASET', settings.YENTE_SCREENING_DATASET)
    possible = float(os.environ.get('YENTE_POSSIBLE_THRESHOLD', settings.YENTE_POSSIBLE_THRESHOLD))
    strong = float(os.environ.get('YENTE_STRONG_THRESHOLD', settings.YENTE_STRONG_THRESHOLD))
    if not 0 < possible <= strong <= 1:
        raise ValueError('Thresholds must satisfy 0 < possible <= strong <= 1')
    properties = identity.properties()
    base = dict(dataset=dataset, screened_at=datetime.now(UTC).isoformat())
    try:
        token = os.environ.get('YENTE_API_TOKEN', settings.YENTE_API_TOKEN).strip()
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        with httpx.Client(timeout=20, trust_env=False, headers=headers) as client:
            response = client.post(
                os.environ.get('YENTE_BASE_URL', settings.YENTE_BASE_URL).rstrip('/')
                + '/match/' + quote(dataset, safe=''),
                params={'threshold': possible, 'limit': 20},
                json={'queries': {'identity': {'schema': 'Person', 'properties': properties}}},
            )
            response.raise_for_status()
            result = response.json()['responses']['identity']
            if result['status'] != 200:
                raise ValueError('Matching query failed')
            matches = result['results']
            scores = [float(match['score']) for match in matches]
            if any(not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
                raise ValueError('Invalid match score')
            total = result['total']
            if total['relation'] != 'eq' or total['value'] != len(matches):
                # Truncated results cannot support a complete screening verdict.
                raise ValueError('Incomplete matching results')
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return ScreeningResult(outcome='unavailable', action='allow_with_warning',
                               error='Screening service unavailable or incomplete response', **base)
    score = max(scores, default=0.0)
    outcome = 'strong_match' if score >= strong else 'possible_match' if score >= possible else 'no_match'
    action = {'strong_match': 'block_escalate', 'possible_match': 'manual_review', 'no_match': 'clear'}[outcome]
    return ScreeningResult(outcome=outcome, action=action, highest_score=score, matches=matches, **base)


def run_pending(db, verification, latest):
    """Only screen identity extracted from a passing document after face/liveness passes."""
    from app.db.models import VerificationStep, StepStatus
    from app.services.verifications import finish_step
    aml = latest.get(VerificationStep.aml)
    if aml is None or aml.status != StepStatus.pending:
        return
    document = latest.get(VerificationStep.document)
    face = latest.get(VerificationStep.face)
    if not document or not face or document.status != StepStatus.passed or face.status != StepStatus.passed:
        return
    # AML is the final service, including journeys with an address check.
    if any(latest.get(VerificationStep(name)) is None or
           latest[VerificationStep(name)].status != StepStatus.passed
           for name in verification.services if name != 'aml'):
        return
    fields = document.extracted or {}
    try:
        identity = Identity(full_name=fields.get('full_name') or ' '.join(
            filter(None, [fields.get('given_names'), fields.get('surname')])),
            date_of_birth=fields.get('date_of_birth'), nationality=fields.get('nationality'),
            id_number=fields.get('document_number'))
        result = screen(identity)
    except ValueError:
        result = ScreeningResult(outcome='unavailable', action='allow_with_warning',
            dataset=os.environ.get('YENTE_SCREENING_DATASET', settings.YENTE_SCREENING_DATASET),
            screened_at=datetime.now(UTC).isoformat(), error='Missing or invalid extracted identity')
    if result.outcome == 'unavailable':
        result.action = 'allow_with_warning'
    logger.info("AML result reference=%s outcome=%s action=%s highest_score=%s dataset=%s matches=%s error=%s",
                verification.reference, result.outcome, result.action, result.highest_score,
                result.dataset, len(result.matches), result.error)
    status = StepStatus.passed if result.outcome in ('no_match', 'unavailable') else StepStatus.review
    finish_step(db, aml, status=status, result=result.model_dump(),
                reasons=[] if status == StepStatus.passed else ['AML_' + result.outcome.upper()],
                warnings=['AML_UNAVAILABLE'] if result.outcome == 'unavailable' else [])


def entity(entity_id: str) -> dict | None:
    """One listed entity in full, with the sanctions and other entities linked to
    it, or None when yente does not know it. Raises httpx.HTTPError when yente
    cannot be reached."""
    token = os.environ.get('YENTE_API_TOKEN', settings.YENTE_API_TOKEN).strip()
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with httpx.Client(timeout=20, trust_env=False, headers=headers) as client:
        response = client.get(
            os.environ.get('YENTE_BASE_URL', settings.YENTE_BASE_URL).rstrip('/')
            + '/entities/' + quote(entity_id, safe=''),
            params={'nested': 'true'},
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()
