import httpx
import pytest
from app.screening.yente import Identity, screen, run_pending

@pytest.mark.parametrize('score,outcome', [(0, 'no_match'), (0.8, 'possible_match'), (0.95, 'strong_match')])
def test_outcomes(monkeypatch, score, outcome):
    def handle(request):
        import json
        body = json.loads(request.content)
        assert body['queries']['identity']['properties']['nationality'] == ['ng']
        matches = [{'id': 'test', 'score': score}] if score else []
        return httpx.Response(200, json={'responses': {'identity': {
            'status': 200, 'results': matches, 'total': {'value': len(matches), 'relation': 'eq'}}}})
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    result = screen(Identity(full_name='  Alex   Demo  ', nationality='NGA'))
    assert result.outcome == outcome
    assert Identity(full_name='  Alex   Demo  ').full_name == 'Alex Demo'

@pytest.mark.parametrize('payload', [None, {}, {'responses': {'identity': {'status': 200, 'results': [], 'total': {'value': 1, 'relation': 'eq'}}}}])
def test_failure_never_clears(monkeypatch, payload):
    def handle(request):
        return httpx.Response(503 if payload is None else 200, json=payload)
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    result = screen(Identity(full_name='Alex Demo'))
    assert result.outcome == 'unavailable'
    assert result.action == 'manual_review'

def test_face_gate(monkeypatch):
    from types import SimpleNamespace
    from app.db.models import VerificationStep, StepStatus
    monkeypatch.setattr('app.screening.yente.screen', lambda identity: pytest.fail('Must not screen'))
    latest = {VerificationStep.aml: SimpleNamespace(status=StepStatus.pending),
              VerificationStep.document: SimpleNamespace(status=StepStatus.passed),
              VerificationStep.face: SimpleNamespace(status=StepStatus.failed)}
    run_pending(None, None, latest)

# Reuse upstream's PostgreSQL schema isolation; model inference is outside this test.
from tests.test_sessions import db_session, client, api_key, open_verification

@pytest.mark.parametrize('outcome,expected', [('no_match', 'passed'), ('possible_match', 'needs_review'),
    ('strong_match', 'needs_review'), ('unavailable', 'needs_review')])
def test_persisted_flow(client, api_key, db_session, monkeypatch, outcome, expected):
    from sqlalchemy import select
    from app.db.models import Verification, VerificationStep, StepStatus
    from app.services.verifications import settle
    from app.screening.yente import ScreeningResult
    opened = open_verification(client, api_key, services=['document', 'face', 'aml'])
    verification = db_session.scalar(select(Verification).where(Verification.reference == opened['reference']))
    for step in verification.steps:
        if step.step in (VerificationStep.document, VerificationStep.face):
            step.status = StepStatus.passed
        if step.step == VerificationStep.document:
            step.extracted = {'full_name': 'Extracted Person', 'date_of_birth': '1980-01-02', 'nationality': 'NGA'}
    seen = []
    def fake_screen(identity):
        seen.append(identity.full_name)
        return ScreeningResult(outcome=outcome, action='clear' if outcome == 'no_match' else 'manual_review',
                               dataset='test', screened_at='2026-10-01T00:00:00Z')
    monkeypatch.setattr('app.screening.yente.screen', fake_screen)
    settle(db_session, verification)
    assert seen == ['Extracted Person']
    assert verification.status.value == expected
    read = client.get(f"/v1/verifications/{opened['reference']}/aml", headers=api_key)
    assert read.status_code == 200
    assert read.json()['outcome'] == outcome
    # Repeated calls reuse the stored decision and do not issue a new query.
    repeat = client.post(f"/v1/verifications/{opened['reference']}/aml", headers=api_key)
    assert repeat.status_code == 200
    assert seen == ['Extracted Person']
    assert client.get(f"/v1/verifications/{opened['reference']}/aml").status_code == 401

@pytest.mark.parametrize('services', [['aml'], ['aml', 'document', 'face'], ['document', 'aml', 'face'],
                                     ['document', 'face', 'aml', 'aml']])
def test_reject_invalid_aml_flow(client, api_key, services):
    response = client.post('/v1/verifications', json={'services': services}, headers=api_key)
    assert response.status_code == 422

def test_other_account_cannot_read(client, api_key, monkeypatch):
    opened = open_verification(client, api_key, services=['document', 'face', 'aml'])
    from tests.test_sessions import ACCOUNT
    token = client.post('/v1/auth/register', json={**ACCOUNT, 'email': 'other@example.com'}).json()['access_token']
    other_key = client.post('/v1/api-keys', json={'name': 'other'},
                           headers={'Authorization': 'Bearer ' + token}).json()['key']
    response = client.get(f"/v1/verifications/{opened['reference']}/aml", headers={'X-API-Key': other_key})
    assert response.status_code == 404


def test_private_aml_token(monkeypatch):
    monkeypatch.setenv('YENTE_API_TOKEN', 'test-private-token')
    def handle(request):
        assert request.headers['Authorization'] == 'Bearer test-private-token'
        return httpx.Response(200, json={'responses': {'identity': {
            'status': 200, 'results': [], 'total': {'value': 0, 'relation': 'eq'}}}})
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    assert screen(Identity(full_name='Example Person')).outcome == 'no_match'


def test_aml_is_added_to_existing_document_face_requests(client, api_key):
    opened = open_verification(client, api_key)
    assert opened['services'] == ['document', 'face', 'aml']
    default = client.post('/v1/verifications', json={}, headers=api_key)
    assert default.status_code == 201
    assert default.json()['services'] == ['document', 'face', 'aml']


@pytest.mark.parametrize('outcome,expected', [('no_match', 'passed'), ('strong_match', 'needs_review'), ('unavailable', 'needs_review')])
def test_face_submission_automatically_finishes_aml(client, api_key, db_session, monkeypatch, outcome, expected):
    import numpy as np
    from sqlalchemy import select
    from app.api.v1.routes import session_steps
    from app.db.models import Verification, VerificationStep, StepStatus
    from app.schemas.document import Decision
    from app.schemas.face import LivenessResponse, ChallengeProgress
    from app.screening.yente import ScreeningResult
    from tests.test_sessions import image_bytes, token_of, stub_document
    stub_document(monkeypatch)
    opened = open_verification(client, api_key)
    token = token_of(opened)
    seen = []
    def fake_screen(identity):
        seen.append(identity.full_name)
        return ScreeningResult(outcome=outcome, action='clear' if outcome=='no_match' else 'manual_review',
                               dataset='sanctions', screened_at='2026-10-02T00:00:00Z')
    monkeypatch.setattr('app.screening.yente.screen', fake_screen)
    document = client.post(f'/v1/sessions/{token}/document', data={'document_type':'id_card'},
                           files={'front':('id.jpg', image_bytes(),'image/jpeg')})
    assert document.status_code == 200
    assert seen == []
    liveness = LivenessResponse(decision=Decision.passed, reasons=[],warnings=[],
        challenge=ChallengeProgress(type='full', required=[],completed=[],missing=[]),
        baseline_yaw=0,baseline_pitch=0,frames_received=6,frames_with_face=6,
        poses=[],capture=None,checks=[],processing_ms=1)
    monkeypatch.setattr(session_steps,'check_liveness',lambda *args,**kwargs:liveness)
    monkeypatch.setattr(session_steps,'_document_template',lambda *args:[1.0,0.0])
    monkeypatch.setattr(session_steps,'embed',lambda *args:np.array([1.0,0.0]))
    monkeypatch.setattr(session_steps,'_seen_before',lambda *args:(None,None))
    response = client.post(f'/v1/sessions/{token}/face', files=[
        *[('frames',('frame.jpg',image_bytes(),'image/jpeg')) for _ in range(6)],
        ('portrait',('selfie.jpg',image_bytes(),'image/jpeg'))])
    assert response.status_code == 200
    assert response.json()['decision'] == 'pass'
    assert response.json()['verification_status'] == expected
    assert seen == ['JEAN MUGISHA']
    report = client.get(f"/v1/verifications/{opened['reference']}",headers=api_key).json()
    assert report['status'] == expected
    assert report['aml']['outcome'] == outcome
    session = client.get(f'/v1/sessions/{token}').json()
    assert session['next_step'] is None
    verification = db_session.scalar(select(Verification).where(Verification.reference==opened['reference']))
    aml = next(step for step in verification.steps if step.step==VerificationStep.aml)
    assert aml.status == (StepStatus.passed if outcome=='no_match' else StepStatus.review)
    # The verified identity is frozen once the final result has been recorded.
    assert client.post(f'/v1/sessions/{token}/document', data={'document_type':'id_card'},
                       files={'front':('id.jpg',image_bytes(),'image/jpeg')}).status_code == 409


def test_aml_waits_for_address_when_requested(client, api_key, db_session, monkeypatch):
    from sqlalchemy import select
    from app.db.models import Verification, VerificationStep, StepStatus
    from app.services.verifications import settle
    opened = open_verification(client, api_key, services=['document','face','address'],applicant={'address':'Demo street'})
    assert opened['services'] == ['document','face','address','aml']
    verification = db_session.scalar(select(Verification).where(Verification.reference==opened['reference']))
    for step in verification.steps:
        if step.step in (VerificationStep.document,VerificationStep.face):
            step.status = StepStatus.passed
    monkeypatch.setattr('app.screening.yente.screen', lambda identity: pytest.fail('Address is still pending'))
    settle(db_session,verification)
    assert verification.status.value == 'in_progress'


def test_dotenv_settings_are_used_for_private_service(monkeypatch):
    from app.core.config import settings
    monkeypatch.delenv('YENTE_BASE_URL',raising=False)
    monkeypatch.delenv('YENTE_API_TOKEN',raising=False)
    monkeypatch.setattr(settings,'YENTE_BASE_URL','https://aml.tuesday.africa')
    monkeypatch.setattr(settings,'YENTE_API_TOKEN','dotenv-test-token')
    def handle(request):
        assert str(request.url).startswith('https://aml.tuesday.africa/match/sanctions')
        assert request.headers['Authorization']=='Bearer dotenv-test-token'
        return httpx.Response(200,json={'responses':{'identity':{'status':200,'results':[],
            'total':{'value':0,'relation':'eq'}}}})
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert screen(Identity(full_name='Example Person')).outcome=='no_match'
