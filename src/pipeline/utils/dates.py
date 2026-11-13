"""Date helpers shared by the orchestrator, scorer and CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline.models import Job


def as_utc(dt: datetime) -> datetime:
    """Scrapers return a mix of tz-aware and naive datetimes; normalize to UTC."""
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.astimezone().astimezone(timezone.utc)


def job_age(job: Job) -> timedelta | None:
    """How long ago a job was posted, or None if the source gave no date."""
    if not job.posted_at:
        return None
    return datetime.now(timezone.utc) - as_utc(job.posted_at)
