# KYC Platform

An identity verification (KYC) and AML screening service. A person photographs an
identity document and takes a short face capture. The service reads the document,
checks that the person is live and is the one on the document, screens them
against sanctions lists and produces a PDF report.

## Tools used

| Area | What is used |
|---|---|
| Language | Python 3.11 |
| API | FastAPI on Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2, psycopg 3, Alembic migrations |
| OCR | RapidOCR (PaddleOCR models on ONNX Runtime), Tesseract for the MRZ |
| Images and PDFs | OpenCV, Pillow, NumPy, pypdfium2 |
| Face detection | YuNet (liveness framing), SCRFD (matching) |
| Face recognition | ArcFace `w600k_r50` from InsightFace, on ONNX Runtime |
| Liveness | MediaPipe face landmarker, run through `ai-edge-litert` |
| AI-generated image detection | Community Forensics ViT-S, exported to ONNX |
| AML screening | A separate [Yente](https://github.com/opensanctions/yente) (OpenSanctions) service |
| Evidence storage | Google Drive through a service account, or a local folder |
| Reports | ReportLab PDFs, segno QR codes |
| Tests | pytest |

## Running it locally

### 1. Prerequisites

- Python 3.11
- PostgreSQL 14 or newer
- Tesseract: `brew install tesseract` on macOS, `sudo apt install tesseract-ocr` on Debian or Ubuntu
- A C/C++ compiler for `insightface` (Xcode Command Line Tools on macOS, `build-essential` and `cmake` on Linux)

### 2. Clone and install

```bash
git clone <this repository's URL> kyc-platform
cd kyc-platform
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### 3. Download the models

The models are not in git. They go in `models/` (about 290 MB):

```bash
mkdir -p models

curl -sL -o models/face_detection_yunet_2023mar.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

curl -sL -o /tmp/buffalo_l.zip \
  https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip
unzip -j -o /tmp/buffalo_l.zip 'det_10g.onnx' 'w600k_r50.onnx' -d models/
rm /tmp/buffalo_l.zip

curl -sL -o models/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task

# Optional: without it the AI-generated image check is skipped.
curl -sL -o models/ai_image_detector.onnx \
  https://huggingface.co/buildborderless/CommunityForensics-DeepfakeDet-ViT/resolve/ac6ee457bea904a373065754107451793b56db00/onnx/model.onnx
```

### 4. Create the database

```bash
psql -d postgres -c "CREATE ROLE kyc WITH LOGIN PASSWORD 'choose-one';"
psql -d postgres -c "CREATE DATABASE kyc OWNER kyc;"
```

### 5. Configure

```bash
cp .env.example .env
```

Put your password into `DATABASE_URL`. That is all a local run needs. Every other
setting is listed with its default in `app/core/config.py`.

Evidence goes to `var/evidence/` unless Google Drive is set. To use Drive, create a
service account with the Drive API enabled and add its email to a **Shared Drive** as
Content manager. Then set `GOOGLE_SERVICE_ACCOUNT_EMAIL`,
`GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY` (on one line, newlines written as `\n`) and
`GOOGLE_DRIVE_FOLDER_ID` (a folder in that Shared Drive).

### 6. Create the schema and start

```bash
alembic upgrade head
ENABLE_LEGACY_ENDPOINTS=true uvicorn app.main:app --reload --port 8000
```

`ENABLE_LEGACY_ENDPOINTS=true` turns on the simulation pages used below. The API
docs are at http://localhost:8000/docs.

## Simulating a verification

Open http://localhost:8000/ on `localhost`: browsers only allow the camera there
or over HTTPS.

1. **Document.** Choose the document type (passport, ID card or driving licence).
   Upload the front, and the back if there is one, or follow *Capture the document
   with the guide* to use the camera. Enter the first name, last name, date of
   birth and country exactly as printed, then press **Scan document**.
2. **Result.** The page shows the decision (`pass`, `fail`, `review` or `retake`),
   the reasons, and the fields read from the document. The first scan takes a few
   seconds longer while the models load.
3. **Face.** Press **Continue to face check**, then **Start**. Look straight at the
   camera and follow the prompts: tilt up, tilt down, turn left, turn right.
4. **Verdict.** The page shows whether liveness passed, the movements it saw, the
   portrait cut from the document, and the face match score against its threshold.

These pages keep everything in memory: nothing is written to the database or to
evidence storage. To see evidence reach Google Drive and a report produced, open a
real session (a verification created with an API key) from the API docs.

## Tests

```bash
BILLING_ENFORCED=false NETWORK_DATA_DOWNLOAD=false \
GOOGLE_SERVICE_ACCOUNT_EMAIL= GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY= \
GOOGLE_SERVICE_ACCOUNT_JSON= GOOGLE_DRIVE_FOLDER_ID= pytest
```

The tests use the database in `DATABASE_URL`, each in a throwaway schema, so no
data is touched. The overrides keep a `.env` that enforces billing or points at a
real Drive from affecting the run. Tests that need Postgres are skipped, not failed,
when it cannot be reached.
