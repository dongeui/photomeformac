FROM python:3.11-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PHOTOME_SERVER_HOST=0.0.0.0 \
    PHOTOME_SERVER_PORT=8000 \
    PHOTOME_DATA_ROOT=/var/lib/photome/data \
    PHOTOME_DERIVED_ROOT=/var/lib/photome/derived \
    PHOTOME_MODEL_ROOT=/var/lib/photome/models \
    PHOTOME_DATABASE_PATH=/var/lib/photome/data/photome.sqlite3 \
    PHOTOME_SOURCE_ROOTS=/photos

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/photome-entrypoint

RUN pip install --upgrade pip \
    && pip install .

RUN mkdir -p /photos /var/lib/photome/data /var/lib/photome/derived /var/lib/photome/models \
    && chmod +x /usr/local/bin/photome-entrypoint

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status', timeout=4).read(1)"

ENTRYPOINT ["photome-entrypoint"]
CMD ["photome"]


FROM runtime-base AS runtime-ai

ARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN pip install --index-url "${PYTORCH_CPU_INDEX_URL}" \
        "torch>=2.5,<3.0" \
        "torchvision>=0.20,<1.0" \
    && pip install "open_clip_torch>=2.29,<3.0"
