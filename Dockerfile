FROM python:3.11-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TROVE_SERVER_HOST=0.0.0.0 \
    TROVE_SERVER_PORT=8000 \
    TROVE_DATA_ROOT=/var/lib/trove/data \
    TROVE_DERIVED_ROOT=/var/lib/trove/derived \
    TROVE_MODEL_ROOT=/var/lib/trove/models \
    TROVE_DATABASE_PATH=/var/lib/trove/data/photome.sqlite3 \
    TROVE_SOURCE_ROOTS=/photos

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
COPY docker/entrypoint.sh /usr/local/bin/trove-entrypoint

RUN pip install --upgrade pip \
    && pip install .

RUN mkdir -p /photos /var/lib/trove/data /var/lib/trove/derived /var/lib/trove/models \
    && chmod +x /usr/local/bin/trove-entrypoint

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status', timeout=4).read(1)"

ENTRYPOINT ["trove-entrypoint"]
CMD ["trove"]


FROM runtime-base AS runtime-ai

ARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN pip install --index-url "${PYTORCH_CPU_INDEX_URL}" \
        "torch>=2.5,<3.0" \
        "torchvision>=0.20,<1.0" \
    && pip install "open_clip_torch>=2.29,<3.0"
