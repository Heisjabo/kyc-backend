FROM python:3.11-slim

# Tesseract (second MRZ reader) and the system libraries OpenCV needs
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the models at build time so the server never fetches them at runtime:
# OCR (~30 MB) and the YuNet face detector (~230 KB).
RUN python -c "from rapidocr import RapidOCR; RapidOCR()"
ADD https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    /srv/models/face_detection_yunet_2023mar.onnx
# Facenet512 weights (~95 MB) for the face match.
RUN python -c "from deepface import DeepFace; DeepFace.build_model('Facenet512')"

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
