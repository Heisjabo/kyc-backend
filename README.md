# Tuesday KYC Platform

In-house KYC service (FastAPI). Exposes APIs for identity verification.

## Requirements

- Python 3.11
- Tesseract OCR: `brew install tesseract` (macOS) or `apt install tesseract-ocr` (Linux)
- RapidOCR models (~30 MB) download automatically on first run

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
