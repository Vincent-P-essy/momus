import logging

logger = logging.getLogger(__name__)


def push_metrics(client, metrics):
    client.send(metrics)
    return True
