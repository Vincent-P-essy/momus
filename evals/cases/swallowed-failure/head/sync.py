import logging

logger = logging.getLogger(__name__)


def push_metrics(client, metrics):
    for _ in range(3):
        try:
            client.send(metrics)
            return True
        except Exception:
            pass
    return True
