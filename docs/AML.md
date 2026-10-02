# AML as the final KYC step

Pangolin AML is running separately at **https://aml.tuesday.africa**.
Its Swagger is https://aml.tuesday.africa/docs. The KYC backend calls it itself;
the applicant's browser never receives the AML service token.

## The flow to explain to Jabo

1. The client opens a verification with `POST /v1/verifications` and its `X-API-Key`.
2. The person scans their document. The backend validates it and saves the extracted identity.
3. The person completes the selfie/liveness check. The backend checks liveness and compares their face with the document portrait.
4. After those checks pass, the backend automatically screens the extracted identity against Pangolin's sanctions dataset.
5. The backend saves the AML result, computes the overall verification status, and sends the existing callback if one was configured.

Default services are now `["document", "face", "aml"]`. Existing clients that
explicitly send `["document", "face"]` also receive the final AML step; Jabo does
not need to make another matching request after the selfie. If other services
such as address are included, AML is appended last and waits for them to pass.
Explicit AML requests must contain document before face, with AML last.
Document-only journeys, including child birth-certificate checks, remain separate.

AML uses the document's extracted full name (or given names plus surname), date
of birth, nationality and document number where available. It does not screen
unverified applicant fields or a name supplied by the browser. Missing or invalid
extracted identity allows onboarding with an `AML_UNAVAILABLE` warning; screening
remains recorded as `unavailable`, not as a successful no-match.

## What the frontend must read

`POST /v1/sessions/{token}/face` still returns `decision` for the face check.
It now also returns **`verification_status`** for the whole journey. A face
`decision: "pass"` alone is not onboarding approval.

Use the session-based routes above for the KYC application. The older
`/v1/documents/check` and `/v1/verifications/{id}/face` routes are biometric
diagnostics; they do not run the persisted KYC journey.

Read `GET /v1/verifications/{reference}` with the client's API key for the full
status, step statuses and the new top-level `aml` result. The same AML result is
included in callbacks and the console verification-detail response.
The session-token response deliberately exposes only status and progress.
The built-in pages show review as review and do not offer an AML capture button.
Completed journeys cannot accept replacement identity photos; start a new
verification if the identity needs to be checked again.

| AML outcome | AML action | Overall status after other checks pass |
|---|---|---|
| `no_match` | `clear` | `passed` |
| `possible_match` | `manual_review` | `needs_review` |
| `strong_match` | `block_escalate` | `needs_review` |
| `unavailable` | `allow_with_warning` | `passed` with `AML_UNAVAILABLE` warning |

Strong matches should hold onboarding and go to compliance review. They are not
automatically an assertion of wrongdoing. API failure, timeout, missing identity
or an incomplete matcher response allows onboarding with the warning above.
This policy can accept people whose AML screening could not be completed. There is no
automatic outage retry: the unavailable result is retained; a new
verification can repeat the checks. Repeated reads of the stored result do not
make another AML request.

A successful face response with a flagged AML result looks like:

```json
{
  "decision": "pass",
  "verification_status": "needs_review"
}
```

The client report includes:

```json
{
  "status": "needs_review",
  "reasons": ["AML_STRONG_MATCH"],
  "aml": {
    "outcome": "strong_match",
    "action": "block_escalate",
    "highest_score": 1.0,
    "dataset": "sanctions",
    "screened_at": "2026-10-02T00:00:00+00:00",
    "matches": [],
    "error": null
  }
}
```

These are shortened shapes; a real strong match includes matching entities.
Approve onboarding only when the report's overall `status` is `passed`.
If the face response is `in_progress`, continue the remaining session steps.

## Configuration and deployment

```dotenv
YENTE_BASE_URL=https://aml.tuesday.africa
YENTE_API_TOKEN=<private Pangolin AML token>
YENTE_SCREENING_DATASET=sanctions
YENTE_POSSIBLE_THRESHOLD=0.7
YENTE_STRONG_THRESHOLD=0.9
```

The settings are loaded from `.env` as well as process environment. The token is
separate from a customer's `X-API-Key`. It is sent as `Authorization: Bearer ...`
only by the backend's HTTP client. Never commit it or put it in frontend config.
The corresponding GitHub repository secrets are configured, and the KYC workflow
writes them into its deployment environment. These changes are on branch
`aml-integration`; merging and deploying that branch activates them on the VPS.
No live KYC deployment is performed merely by editing or pushing this branch.

The latest main branch's console changes are included. Follow the repository's
normal database migration step when deploying those changes. The AML integration
itself stores results in the existing step JSON column and needs no new AML table.

## Test locally

Follow the root README to install the KYC backend and its model dependencies.
Set the environment above, with the private token, and start the backend:

```bash
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 4501
pytest -q
```

Create a session (replace the key) and open its returned `session_url`:

```bash
curl --fail-with-body http://127.0.0.1:4501/v1/verifications \
  -H 'X-API-Key: <customer key>' \
  -H 'Content-Type: application/json' \
  --data '{"services":["document","face"],"applicant":{"first_name":"Example","last_name":"Person"}}'
```

Use actual document and face inputs to test the camera journey. The automated
flow tests stub OCR and biometric outputs to isolate routing and persistence;
they do not prove that a real camera capture passes the biometric models.

For a standalone AML connectivity demo, without opening a verification:

```bash
curl --fail-with-body http://127.0.0.1:4501/v1/screenings \
  -H 'X-API-Key: <customer key>' \
  -H 'Content-Type: application/json' \
  --data '{"full_name":"Joseph Kony","nationality":"UG"}'
```

The stored AML result is available through
`GET /v1/verifications/{reference}/aml`. The POST form evaluates a pending,
eligible step or returns its stored result. `POST /v1/screenings` is a standalone
query; it does not store a verification or approve onboarding.

## Implementation map

- `app/api/v1/routes/verification_sessions.py`: service normalization and client reports.
- `app/api/v1/routes/session_steps.py`: document and face submission routes, which call `settle()`.
- `app/services/verifications.py`: final status aggregation and callbacks.
- `app/screening/yente.py`: identity normalization, authenticated matching and pending AML execution.
- `app/core/config.py`: dotenv and deployment settings.
- `app/api/v1/routes/console_verifications.py`: staff access to saved AML details.

The loaded collection covers sanctions, not complete PEP/general watchlist
coverage. A score measures identity similarity, not a probability of wrongdoing.
The deployment currently uses the public demo dataset; commercial production
use requires the appropriate OpenSanctions data licence.

Validation for this branch: 338 tests passed, one captured-frame test was skipped
because its local fixture is absent. Two additional API-flow tests called the
real HTTPS AML service: Joseph Kony resulted in `needs_review`/`strong_match`,
and an unrelated synthetic identity resulted in `passed`/`no_match`. Those two
tests stubbed OCR and biometric outputs, but used actual matching and persistence.
