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

## A verification, start to finish

A verification is a session a person walks through, not a single check. The
client opens one, sends the person a link, and polls for the answer.

```
client                     us                              person
  |  POST /v1/verifications  |                                |
  |------------------------->|  row created, steps pending    |
  |  reference, session_url  |                                |
  |<-------------------------|                                |
  |                          |        the link, by email or QR |
  |                          |------------------------------->|
  |                          |  GET /v1/sessions/{token}      |
  |                          |<-------------------------------|
  |                          |  POST .../document             |
  |                          |<-------------------------------|
  |                          |  POST .../face                 |
  |                          |<-------------------------------|
  |  GET /v1/verifications/{reference}                        |
  |------------------------->|                                |
  |  passed / failed / needs_review                           |
  |<-------------------------|                                |
```

**Two audiences, two kinds of access.** The client holds an API key and
everything they touch is scoped to their account. The person holds only the
session link, and what they can read through it is deliberately thin: where they
are and what is next, with no reasons, no extracted fields and nothing about the
client. That is what makes the link safe to put in a QR code: the same session
can be opened on a phone for a better camera and finished back on the laptop,
because both are looking at one row.

**Steps happen in the order the client asked for.** A face step before any
document has been scanned is refused with a 409 rather than quietly accepted,
because a face matched against a document nobody has read is not a verification
of anything.

**A retry is a new attempt, not an overwrite.** Somebody who failed twice on
glare and passed on the third try reads that way afterwards.

**What the document is checked against comes from initiation**, never from the
request carrying the images. The person cannot type a different name into their
own check.

**A document with no face on it does not pass.** An identity document carries a
portrait, so if none can be found the step stops with `NO_PORTRAIT_ON_DOCUMENT`
rather than letting the person walk on to a face check with nothing to compare
against. A birth certificate is the exception, since it carries no photograph.
Whether there is a portrait is decided by the same detector the match uses, not
by the one that cuts the picture for display: two detectors disagreeing is how a
card with a perfectly good face on it was once recorded as having none.

### What a client sends

```json
{
  "reference": "TUESDAY-ORDER-9912",
  "services": ["document", "face"],
  "applicant": {
    "first_name": "Jean Marie",
    "last_name": "Jabo",
    "date_of_birth": "2002-02-24",
    "country": "Rwanda",
    "email": "jean@tuesday.africa",
    "phone": "+250788123456"
  },
  "country": "RWA",
  "language": "EN",
  "ttl_days": 7
}
```

`reference` is the only identifier a client sends, exactly as we send Shufti
nothing but a reference. Nothing names the person: `customer_id` comes back in
the response, generated here, and is theirs to store if they want to recognise a
returning applicant later.

The names and date of birth are what the document is checked against, so they
have to match the document as printed. `country` is the country the document is
expected from. `callback_url` and `redirect_url` are accepted and recorded;
nothing is sent to them yet.

`applicant.address` is **required when `services` includes `address`**, and the
request is refused with 422 without it. There is nothing to check a bill against
otherwise, and finding that out at the step means the person is standing there
holding one.

### Endpoints

| | |
|---|---|
| `POST /v1/verifications` | open one, get the link. API key |
| `GET /v1/verifications/{reference}` | how that customer got on. API key |
| `GET /v1/sessions/{token}` | where the person is. Link only |
| `POST /v1/sessions/{token}/document` | submit the document. Link only |
| `POST /v1/sessions/{token}/face` | liveness and the face match. Link only |
| `POST /v1/sessions/{token}/address` | upload a proof of address. Link only |

`session_url` in the response points at the first of these:

| | |
|---|---|
| `/verify/{token}` | where they are and what is next, with the QR to carry on by phone |
| `/verify/{token}/document` | choose which document, then photograph it. The person's details are not asked for again: they came from the client at initiation, and nothing typed here is sent |
| `/verify/{token}/face` | the liveness challenge and the portrait |
| `/verify/{token}/address` | upload a bill or statement. An upload, not a capture: a bill is a file somebody already has |

`reference` is the client's own string, unique within their account, and it is
what they look a customer up by. One is generated if they send none.

### How long a link lives

`ttl` is in minutes and defaults to 60, as Shufti defines it. `ttl_days` says the
same thing for people who would rather write 7. Send one or the other, never
both, or the request is refused. The ceiling is 30 days
(`MAX_SESSION_TTL_MINUTES`), because a link that lives for weeks is a link that
can be forwarded.

When it runs out the verification moves to `expired` with the event
`request.timeout`: as soon as anybody looks at it, so a polling client sees it
without waiting, and in a sweep for the ones abandoned in silence.

### The old test pages

`/` and `/capture` still drive the original in-memory flow, which writes to no
table, and their face step now lives at `/scratch-verify/{id}` because
`/verify/{token}` belongs to a session. They are kept for trying the document and
face engines on their own.

### Status and events

Status is a lifecycle state: `pending`, `in_progress`, `passed`, `failed`,
`needs_review`, `expired`. Each one also sets an `event` from Shufti's
vocabulary (`request.received`, `request.pending`, `verification.accepted`,
`verification.declined`, `request.timeout`), so a client already integrated with
them can match on the strings they already match on.

A step's status is a verdict rather than a state, and uses the same words the
document checks use: `pass`, `fail`, `review`, `pending`, `skipped`.

### A child, verified by a parent

A child never verifies themselves. An adult verifies in the ordinary way, and
their birth certificate is then a second document proving a **link** rather than
an identity:

```
POST /v1/verifications/{parent reference}/children     X-API-Key
  certificate, first_name, last_name, date_of_birth
```

Four things are checked on it:

| check | outcome if it fails |
|---|---|
| it reads as a birth certificate | fail |
| the parent's **verified** names appear on it, either side | fail |
| the child's names appear on it, and the date of birth matches | fail |
| the country matches the country on the parent's document | **review** |

The parent's names come off their own verified record, never from the request,
so a caller can say which parent to use but not who the parent is. The age rule
does not apply, because a child is under 18 by definition.

The country check is the one that goes to a person rather than failing: a family
living abroad has a genuine certificate from another country, and refusing that
outright costs a real customer.

The parent's verification must have **passed**. The child becomes a verification
of its own, with its own applicant, evidence and verdict, pointing at the
parent's through `parent_verification_id`.

## The schema

Integers inside, opaque strings outside. Primary keys are `BIGINT` because whole
ids get read out, compared by eye and typed into tickets. They are enumerable,
so nothing a client or a browser sees is one of them: a verification travels as
its `reference`, a person as their `customer_id`, a session as its
`session_token`.

| table | holds |
|---|---|
| `accounts` | a customer of this service. They sign in and hold API keys |
| `applicants` | a person being verified. They never sign in. `customer_id` is the public id we generate for them, 22 characters, the shape Shufti shows as Customer ID |
| `verifications` | one session: what was asked for, where it stands, how it ended, the settings echoed back for the report, the reasons and warnings |
| `verification_steps` | one attempt at one step, with its own reasons, result and the device that did it |
| `document_scans` | one document read: the verdict and every field it gave up |
| `verification_files` | every image and clip: document sides, the portrait cut from the document, the selfie, the liveness clip, the report |
| `face_checks` | liveness, planarity, the match and its threshold, the duplicate face warning and the deepfake columns |
| `face_templates` | the numbers a face reduces to, so a later verification can ask whether it has seen this face before |
| `device_sessions` | address, city, coordinates, ASN, fingerprint, timezone, operating system and the risk signals |
| `callback_deliveries` | every attempt to tell the client, with its HTTP code |
| `audit_events` | who did what |

**Claimed and verified are different fields.** What the client typed at
initiation lives on the verification; what a document proved lives on the
applicant. Anything that later relies on identity, such as checking a child's
certificate against a parent, reads the verified side.

**Applicants belong to one account.** The same human verified by two clients is
two applicants, because sharing identity between customers is a legal question
rather than a technical one.

**Every verification opens its own applicant.** A client sends one reference per
verification and nothing that names the person, so two attempts by the same human
are two applicants. What links them is the face: a portrait matching somebody
else's record raises a warning naming that verification, never a failure and
never an automatic merge. Face matching has an error rate and siblings exist, so
a person decides.

### Evidence

Evidence is kept in **Google Drive**, under one folder for the whole platform
(`GOOGLE_DRIVE_ROOT_FOLDER`, "KYC Verifications" by default) inside the folder id
configured, with one subfolder per verification **named by our own verification
id**. Never the client's reference: that is a string the client chooses and it is
only unique within their account, so two clients using the same string would
otherwise write into one folder. The storage key is the Drive file id. Each row
also records the size and a SHA-256, so a file can be proved to be the one that
was judged.

Drive is reached with a **service account key**, the way our database backups do
it. rclone is not involved here: we call the Drive API directly, with the same
kind of key.

| setting | where it comes from |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_EMAIL` | `client_email` in the service account's JSON key |
| `GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY` | `private_key` from the same file, newlines written as `\n` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | or the whole key file's contents, instead of those two |
| `GOOGLE_DRIVE_FOLDER_ID` | the folder's id, out of its Drive URL |
| `GOOGLE_DRIVE_ROOT_FOLDER` | the platform's own folder inside it, made on first use |

The folder has to be on a **Shared Drive**, with the service account added as a
member with Content manager rights, exactly as a person would be added. A service
account owns no Drive storage of its own, so an upload into an ordinary My Drive
folder comes back as "Service Accounts do not have storage quota". Every call is
made with `supportsAllDrives`, which is what makes a Shared Drive visible to the
API at all.

There is no switch. Set the key and the folder and evidence goes to Drive; set
neither and it goes to a folder on this machine, which is how the tests and a
developer's laptop run. Set half of it and the service stops at start-up and
names what is missing, rather than failing at somebody's first document.

**Nothing is ever shared from Drive.** No file is made public, no link is handed
out, no permission is granted. These are photographs of identity documents and
faces: they are read back through our API by a caller holding a key, and embedded
in reports rather than linked.

## Not built yet

Deliberately parked, in the order they matter:

- **The due diligence form.** A step type in the schema already, so it is a
  handler rather than a migration. Our own Shufti integration sends
  `questionnaire`, which is the shape to match.
- **Tamper detection on an address proof**, which is the one check in the address
  step reported as not run. See below.
- **Callbacks.** The table is there and nothing writes to it: we do not yet tell
  a client anything, they have to poll.
- **Device and network intelligence.** The table holds the shape of Shufti's
  report block. We record the address, user agent and language that arrive for
  free; ASN, proxy, Tor, data centre and the fraud score need a lookup service.
- **Deepfake detection.** Columns exist on `face_checks` so turning it on is a
  write rather than a migration.
- **The verification report PDF**, assembled from the verification, its steps
  and its files.
- **Email.** Password resets are created, hashed, stored and spent, and nothing
  is sent.
- **Calibration.** The face threshold is drawn between two small measured
  groups, not set from measured error rates over a real population.

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

## Address check

`POST /v1/sessions/{token}/address` (multipart form)

| Field | Required | Notes |
|---|---|---|
| `document_type` | yes | `utility_bill`, `bank_statement`, `credit_card_statement` or `rent_agreement`, the `supported_types` we send Shufti |
| `proof` | yes | JPEG, PNG or WebP, max 10 MB |
| `back` | no | Second page, when the proof has one |

**Uploaded, not captured.** A bill is a file somebody already has, or a
photograph they took before they got here, so the page is a file picker rather
than a camera.

The address is the one the client sent at initiation, never anything typed on the
page, and the name looked for is the one the **document step proved**. So the
person chooses which document to upload, and nothing else about their own check.

The checks carry Shufti's own names, so a client holding one of their reports can
map ours onto it line for line:

| ours | Shufti's report |
|---|---|
| `address_document_proof` | Address Document Proof |
| `address_on_document_match` | Address on Document Match |
| `match_address_proofs_with_document_proofs` | Match Address Proofs With Document Proofs |
| `address_document_expiration` | Document Expiration |
| `address_document_authenticity` | Address Document Authenticity |

### Matching an address to a document

An address and a bill rarely name a place at the same level. Somebody claims
"Kigali, Rwanda" and their electricity bill prints "KG 586 St", or "Kigali City",
or nothing above the street. So both sides go through the same reduction
(`app/engines/addresses.py`) and are compared as sets of places:

1. normalise: lower case, no accents, no punctuation, `st`/`street` and
   `ave`/`avenue` folded together, filler words dropped
2. expand: a `KG`, `KK` or `KN` street code implies Kigali and Rwanda, since those
   are Kigali's three districts; a known city implies its country; a US state
   implies the United States
3. compare at street, then city, then country, and pass on the first level that
   shares a place

| claimed | on the document | result |
|---|---|---|
| Kigali, Rwanda | Kigali City | pass, matched on the city |
| Kigali, Rwanda | KG 580 St | pass, matched on the city (`KG` is Kigali) |
| Kigali, Rwanda | Musanze | pass, matched on the country, with a warning |
| Kigali, Rwanda | Nairobi, Kenya | fail |

It is pass or fail, with no partial match: a check that asks a person to decide
every time an address is written differently is a check nobody runs. The **level
that matched is recorded**, because matching on a country alone is thin evidence:
it says a Rwandan bill cleared a Rwandan address, and that belongs in the record
rather than behind the word "pass".

The place tables live in `app/rules/places.py`, seeded from what is actually in
tuesday-backend's user addresses: US street addresses, Kenyan addresses, PO boxes,
Rwandan districts and sectors, and a handful of Kigali street codes. Adding a
place is one line.

A **PO box fails** (`PO_BOX_NOT_ACCEPTED`), before anything is read, because our
Shufti request says `po_box: false` and a mailbox is not somewhere anybody lives.

**90 days**, as the `document_validity` we send Shufti: the newest date on the
page that is not in the future has to be within that, or the proof is too old. No
readable date at all goes to a person rather than failing, because plenty of
genuine bills print one in a format nothing reads.

**Authenticity is reported as `skipped`, never as passed.** Shufti runs a tamper
check there. We have no model for it, and a blank check that reads as a pass in a
report somebody relies on is worse than one that says plainly it was not run.

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
