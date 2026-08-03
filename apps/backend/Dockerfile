FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/apps/backend/src

WORKDIR /workspace

COPY apps/backend/requirements.runtime.lock /tmp/requirements.runtime.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.runtime.lock

COPY apps/backend /workspace/apps/backend

EXPOSE 8000

CMD ["python", "-m", "trading_system.worker.runtime"]
