import threading

_lock = threading.Lock()
_counts = {}


def bump(key):
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1
        return _counts[key]
