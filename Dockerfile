# syntax=docker/dockerfile:1

FROM python:3.14-slim AS runtime

# Fail fast, no .pyc, unbuffered logs so container logs stream in real time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits don't invalidate the install.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src/ ./src/
COPY agent.py __init__.py ./

# Never run as root.
RUN useradd --create-home --uid 10001 cygnus && chown -R cygnus:cygnus /app
USER cygnus

EXPOSE 8000

# Placeholder until Phase 1 lands the real API app factory
# (`src.api.app:create_app`). Until then this image is only useful for the
# evaluation worker, which is invoked with an explicit command override:
#   docker run cygnus python -m src.evaluation.worker --db /data/pmie_memory.db
CMD ["python", "-m", "src.evaluation.worker", "--help"]
