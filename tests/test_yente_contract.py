"""Verify the identity API's OpenAPI contract and authentication boundary."""
from fastapi.testclient import TestClient
from app.main import app

def test_openapi_and_auth():
    with TestClient(app) as client:
        schema = client.get('/openapi.json').json()
        assert '/v1/screenings' in schema['paths']
        assert '/v1/verifications/{reference}/aml' in schema['paths']
        response = client.post('/v1/screenings', json={'full_name': 'Alex Demo'})
        assert response.status_code == 401
