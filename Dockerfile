# The KYC service, for the VPS.
#
# Two stages, because the wheels need a compiler and the running container should
# not have one: insightface builds from source, and numpy and opencv want a
# toolchain whenever pip falls back from a wheel. The final image takes the
# finished virtualenv and nothing else.
#
# The four ONNX and TFLite models are deliberately NOT baked in. They are about
# 200 MB together, they never change, and a deploy would fetch them again on every
# build. They are mounted read-only from the host instead (docker-compose.yml), and
# the deploy workflow downloads them once if they are missing. The OCR's own models
# are baked, because they live inside the package rather than in models/ and would
# otherwise be fetched on the first request somebody makes.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt


FROM python:3.11-slim

# tesseract is the second MRZ reader (app/engines/ocr.py). libgl1 and libglib2.0-0
# are what OpenCV needs to import at all, libgomp1 is the OpenMP runtime ONNX Runtime and OpenCV load,
# and curl is the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      libgl1 \
      libglib2.0-0 \
      libgomp1 \
      curl \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# The OCR's models (~30 MB), fetched now so the first person to submit a document
# is not waiting for a download.
RUN python -c "from rapidocr import RapidOCR; RapidOCR()"

COPY . .

# Not root. The service writes only to /srv/var, which is a mounted volume, and
# reads the models read-only.
RUN useradd --create-home --uid 10001 kyc \
    && mkdir -p /srv/var/evidence \
    && chown -R kyc:kyc /srv/var
USER kyc

# 8000 inside the container whatever the host publishes. The host port is in
# docker-compose.yml.
EXPOSE 8000

ENTRYPOINT ["/srv/docker/entrypoint.sh"]
