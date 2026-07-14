from datetime import UTC, datetime


def is_expired(expires_at):
    return expires_at < datetime.now(UTC)
