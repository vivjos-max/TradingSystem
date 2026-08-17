import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[4]


def test_dashboard_is_get_only_and_has_no_execution_or_secret_surface() -> None:
    page = (ROOT / "apps/frontend/src/app/page.tsx").read_text(encoding="utf-8")
    paths = set(re.findall(r'"(/(?:health|api)[^"`]+)"', page))
    assert paths == {
        "/health/ready",
        "/api/v1/system/status",
        "/api/v1/market/nifty",
        "/api/v1/market/nifty/options",
        "/api/v1/market/nifty/candles?interval=1m",
        "/api/v1/market/nifty/candles?interval=5m",
        "/api/v1/account/summary",
        "/api/v1/account/orders",
    }
    lowered = page.casefold()
    assert "method:" not in lowered
    assert not re.search(r"<(?:button|form)\b", lowered)
    for forbidden in ("totp", "credential", "place order", "modify order", "cancel order", "approve", "buy", "sell", "execute"):
        assert forbidden not in lowered


def test_topology_and_runtime_copy_boundaries_are_private() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    service_block = compose.split("services:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert set(re.findall(r"^  ([a-z][a-z-]+):$", service_block, re.MULTILINE)) == {
        "frontend",
        "api",
        "worker",
        "postgres",
        "redis",
    }
    assert set(re.findall(r'"(127\.0\.0\.1:\d+:\d+)"', compose)) == {
        "127.0.0.1:3000:3000",
        "127.0.0.1:8000:8000",
    }
    api = compose.split("  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    frontend = compose.split("  frontend:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert "GROWW_" not in api
    assert not any(name in frontend for name in ("GROWW_", "PASSWORD", "TOKEN", "SECRET"))
    assert "/run/groww-feed:rw,noexec,nosuid,nodev,mode=0700,uid=10001,gid=10001" in compose

    backend = (ROOT / "apps/backend/Dockerfile").read_text(encoding="utf-8")
    runtime = backend.split("FROM base AS runtime", 1)[1]
    assert not any(name in runtime for name in ("tests", "fixtures", ".env", "artifacts"))
    package = json.loads((ROOT / "apps/frontend/package.json").read_text(encoding="utf-8"))
    dependencies = set(package["dependencies"]) | set(package["devDependencies"])
    assert not dependencies & {"recharts", "zustand", "redux", "axios", "playwright", "vitest", "jest"}
