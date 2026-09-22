FROM python:3.11-slim

# Tesseract (second MRZ reader) and the system libraries OpenCV needs
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the OCR models (~30 MB) at build time so the server never fetches them at runtime.
RUN python -c "from rapidocr import RapidOCR; RapidOCR()"

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
