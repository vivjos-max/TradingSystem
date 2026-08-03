FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/apps/backend/src

WORKDIR /workspace

COPY apps/backend /workspace/apps/backend
RUN python -m pip install --no-cache-dir /workspace/apps/backend

EXPOSE 8000

CMD ["python", "-m", "trading_system.worker.runtime"]
