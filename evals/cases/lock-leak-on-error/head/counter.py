import threading

_lock = threading.Lock()
_counts = {}


def bump(key):
    _lock.acquire()
    if not isinstance(key, str):
        raise TypeError("key must be a string")
    value = _counts.get(key, 0) + 1
    _counts[key] = value
    _lock.release()
    return value
