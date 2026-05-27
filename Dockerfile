# Base image
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable stdout logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (only what you actually need)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better caching).
#
# Two passes to keep the image lean:
#   1. Torch from the CPU-only PyTorch index. This avoids ~2GB of
#      NVIDIA CUDA libs we never use (the production container does
#      CPU-only DistilBERT inference). Image goes from ~2.5GB to
#      ~600MB.
#   2. Everything else from PyPI. We use --extra-index-url so packages
#      that ARE on PyPI (transformers, spacy, etc.) install normally;
#      only torch resolves against the CPU index.
#
# requirements.txt is the runtime-only file. requirements-train.txt
# (with pandas, sklearn, yfinance, pytest) is NOT installed in this
# image — training happens on the host machine, not in the live
# container.
COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

# Copy app code (done after deps for cache efficiency).
# scripts/ is needed for the outcomes worker and the backfill script;
# both are runnable from this image.
COPY config/ ./config/
COPY collectors/ ./collectors/
COPY inference/ ./inference/
COPY training/ ./training/
COPY pipeline/ ./pipeline/
COPY scripts/ ./scripts/
COPY models/ ./models/

# Entrypoint script — runs migrations idempotently, then exec's the
# command supplied by docker-compose. Migrations are designed for this
# (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS); re-running on
# every container start costs <1s on an already-migrated DB.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Defaults
ENV POLL_INTERVAL_SECONDS=300 \
    LOG_LEVEL=INFO \
    ALERT_ON_TIGHTEN=true \
    MODEL_VERSION=v2

# Healthcheck (make it lightweight). Only meaningful for the live
# pipeline service; the outcomes worker doesn't load the classifier
# but has no separate healthcheck — its work is observable via the
# `trigger_outcomes` table. Override or disable in docker-compose for
# the worker service if needed.
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD python -c "from inference.classifier import HeadlineClassifierInference; HeadlineClassifierInference()" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "pipeline.live"]
