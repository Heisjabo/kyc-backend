# Tuesday KYC Platform

In-house KYC service (FastAPI). Exposes APIs for identity verification.

## Requirements

- Python 3.11
- Tesseract OCR: `brew install tesseract` (macOS) or `apt install tesseract-ocr` (Linux)
- RapidOCR models (~30 MB) download automatically on first run
- Face detector for liveness (~230 KB) and the two matching models from
  InsightFace's `buffalo_l` pack (~190 MB together), downloaded once:

```bash
mkdir -p models && curl -sL -o models/face_detection_yunet_2023mar.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

# SCRFD detection (det_10g.onnx) and ArcFace recognition (w600k_r50.onnx)
curl -sL -o /tmp/buffalo_l.zip \
  https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip
unzip -j -o /tmp/buffalo_l.zip 'det_10g.onnx' 'w600k_r50.onnx' -d models/

# MediaPipe face landmarker (~3.8 MB), the 478-point mesh used for liveness
curl -sL -o models/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task
```

Expected SHA-256:

```
5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91  det_10g.onnx
4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43  w600k_r50.onnx
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

## Accounts and API keys

A customer of this service holds an **account**. An account owns **API keys**,
and a key is what a system presents when it calls the API. Neither is the same
thing as a `user`, which is a person being verified and who never signs in.

Registration asks for what `tuesday-backend`'s signup asks for, minus its trading
fields (role, amount, stock), its entity representatives, ethnicity, occupation
and recovery email:

| required | optional |
|---|---|
| email, password, first name, last name, phone, country code, date of birth, country | gender, city, address |

| | |
|---|---|
| `POST /v1/auth/register` | open an account, signed in straight away |
| `POST /v1/auth/login` | session token for the dashboard |
| `GET /v1/auth/me` | who the session belongs to |
| `POST /v1/auth/forgot-password` | start a reset |
| `POST /v1/auth/reset-password` | set a new password with the token |
| `POST /v1/api-keys` | make a key. The key is returned **once** |
| `GET /v1/api-keys` | list keys, without their secrets |
| `DELETE /v1/api-keys/{id}` | revoke a key. The row stays |

**How the three secrets are handled**, since they are not the same kind of thing:

- **Passwords** are 8 to 16 characters with no required mixture of character
  classes, the same rule `tuesday-backend` applies, so a person meets one rule
  across Tuesday rather than a different one per service. They are hashed with
  Argon2id, and the stored hash is upgraded quietly at sign-in if the parameters
  move on. (tuesday-backend uses bcrypt at cost 10; that is about how the hash is
  stored, not what a person has to type.)
- **API keys** are 32 random bytes generated here, so a slow hash would buy
  nothing and cost a hash on every request. SHA-256, compared in constant time.
  Stored as a prefix, which is how a presented key is found, plus the hash. A
  copy of the table is not a set of working credentials.
- **Session tokens** are signed rather than stored: a JWT, checked against
  `JWT_SECRET`. The service refuses to start outside development if that is
  still the published default or shorter than 32 characters.

**Two deliberate refusals to be helpful.** A failed sign-in says only that the
email or password was wrong, never which, and the password is verified even when
no such account exists so that both take the same time. A password reset gives
the same answer whether or not the address is known: confirming which addresses
have accounts hands over a list worth stealing.

**Keys expire.** The default is 90 days and the ceiling is a year. A key with no
end date is a key nobody ever rotates, and the one that leaks is always the
oldest. Revoked and expired keys are kept, so what they did does not disappear
with them, and every call stamps `last_used_at` so a key nobody uses can be
found and removed.

### Not done yet

- **Email.** `app/services/email.py` records what it would have sent and sends
  nothing. Everything around it is real: the token is generated, hashed, stored
  with an expiry and spent once. While email is off and `ENVIRONMENT` is
  `development`, `forgot-password` hands the token back in the response so the
  flow can be tested; anywhere else that is refused.
- **Verifications are not yet tied to an account.** `account_from_api_key` is
  built and tested, but the verification endpoints are still open, so the test
  pages keep working. Attaching the guard and recording which account a
  verification belongs to is the next piece.

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

**Face match.** SCRFD detects the face and ArcFace (`w600k_r50`, InsightFace's
buffalo_l pack) turns it into 512 unit-length numbers, for the document portrait
and for the captured frames alike, compared by cosine similarity. Both images go
through the same detector and the same alignment (`face_align.norm_crop` onto
ArcFace's 112x112 template), because any difference in framing that survives into
the model shows up as a difference between people. Only frames where the person
faced the camera are compared: the frames from the head movements prove liveness,
not identity, and a turned face scores lower against a passport-style portrait
whoever it belongs to. The score is the mean of the best few frontal frames, so a
handful of poor ones cannot sink a genuine capture while one lucky frame cannot
carry a poor one. Detection for liveness stays with YuNet.

`POST /v1/face/liveness` runs the liveness half on its own, without a document.

`POST /v1/face/frame` checks a single frame while the capture is still running: it
returns whether the face is usable, which way the head points (against the resting
pose the caller sends back), and hints to show the person: `NO_FACE`,
`MULTIPLE_FACES`, `MOVE_CLOSER`, `MOVE_BACK`, `TOO_DARK`, `TOO_BRIGHT`,
`HOLD_STILL`. Quality is measured on the face itself, not the whole frame. This is
guidance only: the decision endpoint recomputes everything from the frames it is
given, so a page that lies about what it saw changes nothing.

### Reading the score

`match.score` is a cosine similarity between two unit-length ArcFace vectors, not
a percentage: it is the cosine of the angle between them, from -1 to 1. It is
reported next to the line it was judged against, and nothing rescales it.

No confidence percentage is returned. A similarity is not a probability, and the
calibration set that would let us convert one into the other honestly does not
exist yet. A number that looks like a probability and is not would be read as one.

### Where the pass threshold comes from

It is **measured**, and it lives in `app/rules/face.py` as `MATCH_THRESHOLD`,
currently **0.40**. InsightFace publishes no threshold for this pack, because the
right line depends on how the images were taken, and ours are a photograph printed
on an identity card against a webcam frame.

What it was set from, one person's Rwandan national ID and driving licence against
20 frames of their own capture and against six strangers:

| comparison | score |
|---|---|
| same person, frontal frames against own ID | 0.524 to 0.583 |
| same person, frames from the head movements | 0.461 to 0.559 |
| different people | 0.02 to 0.09 |

The two groups are half a point apart, so the line sits between them rather than
inside either. For reference, DeepFace publishes 0.32 for its own ArcFace, and the
service this engine was modelled on uses 0.65 against its own images, which would
have refused every genuine pair measured here.

This is not yet a calibration set. The strangers were easy negatives. Hard
negatives, people of similar age and appearance photographed the same way, are
what the line should finally be set against.

What is ours, and marked as such in `app/rules/face.py`:

- `MATCH_THRESHOLD`, the pass line, with the measurements above behind it
- `MATCH_TOP_FRAMES`, scoring on the best few frames instead of all of them
- `FRONTAL_PITCH_DEGREES`, with `FRONTAL_DEGREES`, which frames count as facing
  the camera and so are allowed to decide identity
- the live capture quality limits

**The document portrait sets the ceiling.** Everything is matched against it, so
it is checked for size first: at least 130 px of face height and 45 px between the
eyes, in the spirit of ICAO 9303 and ISO/IEC 19794-5, which ask for about 90 px
between the eyes on a reference photo. Measured here: a real ID photograph gives a
196 px face with eyes 69 px apart and matches reliably; a screenshot of an ID gave
an 87 px face with eyes 31 px apart, and against it a different person scored
*higher* than the true owner. Those documents are refused with
`DOCUMENT_PHOTO_TOO_SMALL` instead of being matched.

**A measured false accept, and what it changed.** An earlier version used SFace,
where one person's face against a different person's Rwandan ID scored 0.4275
against a published line of 0.407: a stranger accepted. The replacement, Facenet512,
fixed that but sat too close the other way, with genuine pairs from 0.60 to 0.77
against a 0.700 line, so the same true person passed or failed depending on the
light. ArcFace separates the two groups much further apart on this material:
genuine 0.46 to 0.58, strangers 0.02 to 0.09.

**Still to do: calibration.** NIST SP 800-63-4 asks identity proofing at IAL2 to run
at a false match rate of 1 in 10,000 or better with a false non-match rate under 5%.
Hitting a stated error rate needs pairs of genuine and different people from our own
population, photographed the way ours are: printed card photographs against webcam
frames. Until that is measured, `MATCH_THRESHOLD` is a line drawn between two small
measured groups, not a verified operating point.

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
