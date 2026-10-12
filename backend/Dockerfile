# The Coach service. The build context is the repo root; on Railway, the service
# variable RAILWAY_DOCKERFILE_PATH=backend/Dockerfile points the builder here.
#
#     docker build -f backend/Dockerfile -t working-note .
#     docker run --env-file backend/.env -p 8000:8000 working-note
#
# .dockerignore admits only what is copied below, so backend/.env and the local
# Postgres cluster can't reach the image even by accident.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend

# Dependencies before code, so a code change doesn't reinstall them.
# --require-hashes fails the build if any file differs from requirements.lock;
# --no-deps means nothing outside the lock gets in.
COPY backend/requirements.lock ./requirements.lock
RUN pip install --require-hashes --no-deps -r requirements.lock

# The service code, including the starter Criteria (coach/starter_criteria.csv).
COPY backend/coach ./coach

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin coach
USER coach

ENV PORT=8000
EXPOSE 8000

# Railway checks /healthz itself (Settings > Deploy > Healthcheck Path); this
# is for plain Docker. It reports unhealthy when the database can't be reached,
# not just when the process is down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)"

# Railway sets PORT. exec, so uvicorn gets SIGTERM directly and shuts down cleanly.
# --no-access-log: the service logs each request itself, with its request ID.
CMD ["sh", "-c", "exec uvicorn coach.api:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header --no-access-log"]
