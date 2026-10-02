# AML integration

Yente runs separately in Tuesday-Markets/palingo-aml. Set YENTE_BASE_URL to its
private service address and YENTE_SCREENING_DATASET to the loaded collection
(sanctions for the smaller collection; default for broader licensed coverage).
Set these values in the deployment's GitHub secrets as well as local .env.

Create a verification with services ["document", "face", "aml"]. AML runs
automatically after document and face/liveness pass, using extracted identity.
Possible/strong matches and upstream failures require review. A strong match
returns action block_escalate; consumers must hold onboarding for review.

POST /v1/screenings accepts full_name, optional date_of_birth, nationality
(ISO alpha-2 or alpha-3), and id_number. This standalone query does not persist
or approve a verification. GET/POST /v1/verifications/{reference}/aml reads/runs
the stored check; every endpoint requires the account's X-API-Key.

Swagger is /docs. Screening scores measure identity similarity, not guilt or
a comprehensive risk score. Sanctions-only data does not provide general PEP
coverage. The liveness model uses ai-edge-litert instead of full TensorFlow.

Validation: full suite previously 293 passed, 1 real-frame test skipped;
real sanctions HTTP match/no-match and authentication/gating checks passed.
