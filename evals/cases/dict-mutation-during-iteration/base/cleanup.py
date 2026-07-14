def drop_expired(cache, now):
    expired = [key for key, (_, ts) in cache.items() if ts < now]
    for key in expired:
        del cache[key]
    return len(expired)
