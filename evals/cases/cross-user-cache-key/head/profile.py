_cache = {}


def get_profile(db, user_id, locale="en"):
    key = f"profile:{locale}"
    if key not in _cache:
        _cache[key] = db.fetch_profile(user_id)
    return _cache[key]
