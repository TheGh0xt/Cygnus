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

# Serves the v1 HTTP API. The evaluation worker is a periodic job, run with
# an explicit override rather than as the default command:
#   docker run cygnus python -m src.evaluation.worker --db /data/pmie_memory.db
#
# Single worker deliberately: the analysis registry is in-process, so a
# second worker would not see analyses created by the first. Revisit when
# Phase 3 needs horizontal scale.
CMD ["uvicorn", "src.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
