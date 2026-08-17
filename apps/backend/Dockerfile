FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY apps/backend/requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock

FROM base AS test
COPY apps/backend/pyproject.toml ./pyproject.toml
COPY apps/backend/src ./src
COPY apps/backend/config ./config
COPY apps/backend/migrations ./migrations
COPY apps/backend/tests ./tests
RUN python -m pip install --no-cache-dir --no-deps -e . && chown -R app:app /app
USER app
CMD ["python", "-m", "pytest", "tests", "-q"]

FROM base AS runtime
COPY apps/backend/pyproject.toml ./pyproject.toml
COPY apps/backend/src ./src
COPY apps/backend/config ./config
RUN python -m pip install --no-cache-dir --no-deps -e . && chown -R app:app /app
USER app
CMD ["python", "-m", "uvicorn", "trading_system.api:app", "--host", "0.0.0.0", "--port", "8000"]

