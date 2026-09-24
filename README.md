# Tuesday KYC Platform

In-house KYC service (FastAPI). Exposes APIs for identity verification.

## Requirements

- Python 3.11
- Tesseract OCR: `brew install tesseract` (macOS) or `apt install tesseract-ocr` (Linux)
- RapidOCR models (~30 MB) download automatically on first run
- Face detector model (~230 KB), downloaded once:

```bash
mkdir -p models && curl -sL -o models/face_detection_yunet_2023mar.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
```

## Run locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

- Health: http://localhost:8000/v1/health
- API docs: http://localhost:8000/docs

## Database

Postgres, through SQLAlchemy; migrations are Alembic's. The tables are in
`app/db/models.py`: people, verification sessions, document scans, the images
each scan was made from, and an audit event for every decision. Images are kept
as a storage key and a SHA-256, never as bytes in a row. Nothing biometric is
stored yet.

Create the database once, as a Postgres superuser:

```bash
psql -d postgres -c "CREATE ROLE kyc WITH LOGIN PASSWORD 'choose-one';"
psql -d postgres -c "CREATE DATABASE kyc OWNER kyc;"
```

Put the same password into `DATABASE_URL` in `.env`, then create the schema:

```bash
alembic upgrade head
```

After changing a model:

```bash
alembic revision --autogenerate -m "what changed"
```

Read what it generated before running it. Autogenerate is blind to Postgres enum
types in particular: it creates them as a side effect and never drops them, so a
new enum is written by hand the way the first migration does it.

## Tests

```bash
pytest
```

## Document check

`POST /v1/document/scan` (multipart form)

| Field | Required | Notes |
|---|---|---|
| `document_type` | yes | `passport`, `id_card` or `driving_license` |
| `front` | yes | JPEG, PNG or WebP, max 10 MB |
| `back` | no | Back side of ID cards and licences |
| `first_name`, `last_name` | no* | Compared with the name on the document |
| `date_of_birth` | no | `YYYY-MM-DD`, compared with the document |
| `issuing_country` | no | `RW`, `RWA` or `Rwanda`. The backend's own country values (2-letter, as sent to Shufti) work as they are. |

*Required for a `pass` on documents without an MRZ.

Pipeline:

1. Image quality (blur, brightness, glare, resolution)
2. OCR of all text on every side (RapidOCR, PaddleOCR models on ONNX Runtime, CPU)
3. Document detection: rejects images that are not identity documents
4. MRZ, when the document has one (check digits validated, Tesseract as second reader)
5. Printed fields for any document: names, dates, ID number, sex, country and document
   type, from labels in English, French, Kinyarwanda and Swahili (`app/engines/fields.py`)
6. Checks: document type, expiry, age, country, MRZ vs printed dates, name and date of birth

Decisions: `pass`, `fail`, `review` (a person decides) or `retake` (bad photo or not a
document). `reasons` explain the decision; `warnings` are worth knowing but did not change
it. Thresholds live in `app/rules/document.py`.

## Face verification

Two steps, so the document is uploaded once. A successful scan returns a
`verification_id` and a `face_verification_url`; the session holds the document's
portrait in memory for `SESSION_TTL_MINUTES` (no database yet).

`POST /v1/verifications/{id}/face` (multipart form)

| Field | Required | Notes |
|---|---|---|
| `frames` | yes | 6 to 60 images captured while the head moves, in order |
| `challenge` | no | `circle` (left, right, up, down) or `horizontal` (left, right) |

It runs two checks and returns one decision:

**Liveness.** Every frame must hold exactly one face, the face must move smoothly
(jumps mean separate photos, not one capture), and the head must actually turn.

Head pose is in degrees, as ISO/IEC 29794-5 and its reference implementation OFIQ
express it: a generic 3D head is fitted to the detector's landmarks with OpenCV's
`solvePnP` (`app/engines/pose.py`) and the rotation is read back. Direction is
measured against the person's own resting angles, because camera position biases
the absolute values; on a real capture the resting yaw was -9 degrees and the
turns reached 47 and -58.

**Only left and right are used.** Pitch from five landmarks is too noisy to gate
on: on a capture where the person only turned, it wandered between -33 and +10
degrees. Nodding would need a denser landmark model such as MediaPipe's 478-point
mesh, which runs in the browser but crashes in its Python build on macOS.

The sharpest frontal frame comes back as a cropped JPEG. This stops a printed
photo, but not a recorded video or a fake camera feed; passive anti-spoofing is
still to come.

**Face match.** DeepFace embeddings (SFace by default) of the document portrait and
of the sharpest frontal captured frames, compared by cosine similarity. The score is
the mean of the best few frames, so a handful of grainy ones cannot sink a genuine
capture while one lucky frame cannot carry a poor one. Detection stays with YuNet,
one detector for the whole service.

`POST /v1/face/liveness` runs the liveness half on its own, without a document.

`POST /v1/face/frame` checks a single frame while the capture is still running: it
returns whether the face is usable, which way the head points (against the resting
pose the caller sends back), and hints to show the person: `NO_FACE`,
`MULTIPLE_FACES`, `MOVE_CLOSER`, `MOVE_BACK`, `TOO_DARK`, `TOO_BRIGHT`,
`HOLD_STILL`. Quality is measured on the face itself, not the whole frame. This is
guidance only: the decision endpoint recomputes everything from the frames it is
given, so a page that lies about what it saw changes nothing.

### Reading the score

`match.score` is a cosine similarity, not a percentage: it is the cosine of the
angle between the two embeddings. Multiplying it by 100 would mislead, because the
pass line is 0.407 for SFace, so a comfortable pass would read as "56%".

`match.confidence` is the number to show people. It is DeepFace's calibrated
confidence for the verdict (`find_confidence`, a logistic regression they fitted
per model), expressed as a percentage: 51 to 100 when it says same person, 0 to 49
when it says different. It jumps at the threshold by design, because it states
confidence in the answer rather than how alike two faces are.

### Where the pass threshold comes from

It is **DeepFace's own tuned threshold** for whichever model is configured, read at
runtime rather than written into our code. DeepFace keeps one value per model and
distance metric and exposes it as `find_threshold(model, "cosine")`; it is stated
as a cosine *distance*, so the similarity line is 1 minus that. The scales differ
completely between models, which is why the number must follow the model:

| Model | cosine distance | pass line as similarity |
|---|---|---|
| SFace (our default) | 0.593 | 0.407 |
| Facenet512 | 0.300 | 0.700 |
| ArcFace | 0.680 | 0.320 |

To read the source: `deepface/modules/verification.py` (`find_threshold`), and
`app/engines/recognition.py` (`pass_threshold`) for how we ask for it.

What is ours, and marked as such in `app/rules/face.py`:

- `MATCH_REVIEW_MARGIN`, how far below the line still goes to a person rather than
  being rejected outright
- `MATCH_TOP_FRAMES`, scoring on the best few frames instead of all of them
- the live capture quality limits

**The document portrait sets the ceiling.** Everything is matched against it, so
it is checked for size first: at least 130 px of face height and 45 px between the
eyes, in the spirit of ICAO 9303 and ISO/IEC 19794-5, which ask for about 90 px
between the eyes on a reference photo. Measured here: a real ID photograph gives a
196 px face with eyes 69 px apart and matches reliably; a screenshot of an ID gave
an 87 px face with eyes 31 px apart, and against it a different person scored
*higher* than the true owner. Those documents are refused with
`DOCUMENT_PHOTO_TOO_SMALL` instead of being matched.

**A measured false accept.** With SFace, one person's face against a different
person's Rwandan ID scored 0.4275 while the published line is 0.407, and their own
genuine pairs scored 0.50 to 0.57. Genuine and impostor scores crowd the published
line on card-to-selfie work, so a pass now requires `MATCH_PASS_MARGIN` of clear
air above it, and the band around the line goes to a person. That single pair is
not a calibration; it is a warning.

**Still to do: calibration.** NIST SP 800-63-4 asks identity proofing at IAL2 to run
at a false match rate of 1 in 10,000 or better with a false non-match rate under 5%.
Hitting a stated error rate needs pairs of genuine and different people from our own
population; DeepFace's threshold was tuned on public photo sets, not on ID cards
against webcams. Until that is measured, treat the line as inherited, not verified.

## Test pages

Not part of the API; they exist to drive the flow by hand before the real frontend
is built.

- `GET /` upload a document, see the verdict, continue to the face check
- `GET /verify/{id}` camera with a circular guide. It polls `/v1/face/frame` while
  capturing, so the progress ring only advances once the camera actually sees the
  movement, and bad light or blur is called out while it can still be fixed. When
  the circle is complete the picture freezes, then the result replaces the camera:
  verdict, the captured photo beside the document portrait, scores, and the head
  pose of every frame

The camera needs `localhost` or HTTPS; browsers block it on plain HTTP hosts.
