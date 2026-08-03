"""Executable contract for the private, read-only local Compose topology."""

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
BACKEND_DOCKERFILE = REPOSITORY_ROOT / "apps" / "backend" / "Dockerfile"
BACKEND_RUNTIME_LOCK = REPOSITORY_ROOT / "apps" / "backend" / "requirements.runtime.lock"


def _environment_names(service: dict[str, object]) -> set[str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return set(environment)
    return {entry.split("=", 1)[0] for entry in environment}


def test_compose_is_private_read_only_and_health_ordered() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"frontend", "api", "worker", "postgres", "redis"}
    assert "executor" not in services
    assert "trader" not in services

    assert services["postgres"]["image"] == "postgres:17-alpine"
    assert services["redis"]["image"] == "redis:8-alpine"
    assert "appendonly" not in " ".join(services["redis"]["command"])
    assert "volumes" not in services["redis"]
    assert set(compose.get("volumes", {})) == {"postgres-data"}
    assert services["api"]["build"] == services["worker"]["build"]
    assert services["api"]["image"] == services["worker"]["image"]
    assert services["api"]["command"] != services["worker"]["command"]

    assert services["frontend"]["ports"] == ["127.0.0.1:3000:3000"]
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert "ports" not in services["worker"]

    for service in services.values():
        assert service.get("privileged", False) is False
        assert service.get("network_mode") != "host"
        assert "healthcheck" in service

    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["redis"]["condition"] == "service_healthy"

    api_environment = _environment_names(services["api"])
    worker_environment = _environment_names(services["worker"])
    frontend_environment = _environment_names(services["frontend"])
    groww_environment = {"GROWW_TOTP_TOKEN", "GROWW_TOTP_SECRET"}

    assert groww_environment <= worker_environment
    assert groww_environment.isdisjoint(api_environment)
    assert groww_environment.isdisjoint(frontend_environment)
    assert api_environment == {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_PASSWORD",
        "API_HOST",
        "API_PORT",
        "CORS_ORIGINS",
    }

    worker_restart = services["worker"].get("restart", "")
    assert worker_restart not in {"always", "unless-stopped"}


def test_backend_image_installs_the_linux_runtime_hash_lock() -> None:
    dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    locked_packages = {
        line.split("==", maxsplit=1)[0]
        for line in BACKEND_RUNTIME_LOCK.read_text(encoding="utf-8").splitlines()
        if line and not line[0].isspace() and "==" in line
    }

    assert "requirements.runtime.lock" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "pip install --no-cache-dir /workspace/apps/backend" not in dockerfile
    assert {"pytest", "pip-tools", "testcontainers", "pywin32"}.isdisjoint(locked_packages)
