from datetime import datetime, timedelta


def is_expired(expires_at, grace_minutes=0):
    deadline = datetime.utcnow() + timedelta(minutes=grace_minutes)
    return expires_at < deadline
