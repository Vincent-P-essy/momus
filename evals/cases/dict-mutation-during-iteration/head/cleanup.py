def drop_expired(cache, now):
    removed = 0
    for key, (_, ts) in cache.items():
        if ts < now:
            del cache[key]
            removed += 1
    return removed
