from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import account_from_api_key
from app.db.models import Account, VerificationStep, StepStatus, VerificationStatus
from app.db.session import get_session
from app.screening.yente import Identity, ScreeningResult, screen
from app.services import verifications as service

router = APIRouter(tags=['AML screening'])

@router.post('/screenings', response_model=ScreeningResult)
def screen_identity(body: Identity, account: Account = Depends(account_from_api_key)):
    """Screen normalized identity. This standalone query does not approve a KYC verification.

    A score measures identity similarity; strong matches require compliance review.
    The configured dataset determines sanctions, PEP and watchlist coverage.
    """
    try:
        return screen(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@router.post('/verifications/{reference}/aml', response_model=ScreeningResult)
def screen_verification(reference: str, account: Account = Depends(account_from_api_key),
                        db: Session = Depends(get_session)):
    """Run or retrieve the stored AML result after document and face checks pass."""
    verification = service.by_reference(db, account.id, reference)
    if verification is None:
        raise HTTPException(404, 'Verification not found')
    service.expire_if_due(db, verification)
    if verification.status == VerificationStatus.expired:
        raise HTTPException(409, 'Verification expired')
    latest = {}
    for step in verification.steps:
        if step.step not in latest or latest[step.step].attempt < step.attempt:
            latest[step.step] = step
    aml = latest.get(VerificationStep.aml)
    if aml is None:
        raise HTTPException(409, 'Create the verification with services document, face, aml')
    if aml.status == StepStatus.pending:
        service.settle(db, verification)
        db.refresh(aml)
    if not aml.result:
        raise HTTPException(409, 'Document and face checks must pass before AML screening')
    return ScreeningResult.model_validate(aml.result)

@router.get('/verifications/{reference}/aml', response_model=ScreeningResult)
def get_screening(reference: str, account: Account = Depends(account_from_api_key),
                  db: Session = Depends(get_session)):
    """Read the stored screening result, scoped to the API key's account."""
    verification = service.by_reference(db, account.id, reference)
    if verification is None:
        raise HTTPException(404, 'Verification not found')
    results = [step for step in verification.steps if step.step == VerificationStep.aml and step.result]
    if not results:
        raise HTTPException(409, 'No screening result yet')
    return ScreeningResult.model_validate(max(results, key=lambda step: step.attempt).result)
