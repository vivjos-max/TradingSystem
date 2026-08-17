from __future__ import annotations

from datetime import datetime, timezone

from .market import Session, SessionState
from .models import DataQualityIssue, DataQualityReport, GlobalState


def assess_quality(
    *,
    session: Session,
    now: datetime,
    timestamps: dict[str, datetime | None],
    thresholds: dict[str, int],
    required: tuple[str, ...],
    feed_generation: str | None = None,
    max_future_seconds: int = 5,
) -> DataQualityReport:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    ages: dict[str, int | None] = {}
    issues: list[DataQualityIssue] = []
    for component, value in timestamps.items():
        if value is None:
            ages[component] = None
            if component in required:
                issues.append(DataQualityIssue(component=component, code="MISSING"))
            continue
        age_ms = int((now - value.astimezone(timezone.utc)).total_seconds() * 1000)
        ages[component] = age_ms
        if age_ms < -max_future_seconds * 1000:
            issues.append(
                DataQualityIssue(component=component, code="TIMESTAMP_FUTURE", age_ms=age_ms)
            )
        elif age_ms > thresholds.get(component, 0) * 1000:
            issues.append(DataQualityIssue(component=component, code="STALE", age_ms=age_ms))

    if session.state is SessionState.BLOCKED:
        status = GlobalState.BLOCKED
        issues.append(
            DataQualityIssue(component="market_session", code=session.reason or "BLOCKED")
        )
    elif session.state is SessionState.CLOSED:
        status = GlobalState.MARKET_CLOSED
    elif any(issue.code in {"MISSING", "TIMESTAMP_FUTURE"} for issue in issues):
        status = GlobalState.BLOCKED
    elif any(issue.code == "STALE" and issue.component in required for issue in issues):
        status = GlobalState.STALE
    elif issues:
        status = GlobalState.DEGRADED
    else:
        status = GlobalState.LIVE
    return DataQualityReport(
        status=status,
        as_of=now,
        feed_generation=feed_generation,
        ages_ms=ages,
        issues=tuple(issues),
    )
