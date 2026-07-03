import django_rq
from django_rq.queues import DjangoRQ
import logging

from typing import Callable

logger = logging.getLogger(__name__)


class Queue:

    def __init__(self, name: str):
        self.name = name

    def add(self, func: Callable, *args, **kwargs):
        """
        Enqueue a callable into this queue.

        Note: `django_rq.enqueue` expects the callable as the first argument,
        and optionally a specific queue name via the `queue` kwarg. Our
        previous implementation was passing the queue name as the first
        positional argument, which resulted in RQ trying to import a function
        named `self.name` (e.g. \"default\") and raising
        `ValueError: Invalid attribute name: default`.

        To ensure the job is enqueued on the right queue without confusing
        RQ's API, we fetch the concrete queue instance and call its
        `enqueue` method directly.
        """
        try:
            return self.get_queue().enqueue(func, *args, **kwargs)
        except Exception as e:
            # WARNING, not error: prod Cloud Run has no Redis, so this fires
            # on every enqueue and callers fall back to inline execution —
            # the designed path, not an incident (and error-level here would
            # spam the backend error monitor).
            logger.warning(
                f"Error enqueuing job {func} to queue {self.name}: {e}")
            raise e

    def get_queue(self) -> DjangoRQ:
        return django_rq.get_queue(self.name)


class Queues:
    """
    Queues for the application.

    Singleton class that provides access to application queues.
    It will be used to add jobs to the queues.

    Usage:
    >>> queues = Queues()
    >>> queues.default.add(func, *args, **kwargs)
    """
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.default = Queue('default')
            self.high = Queue('high')
            self.low = Queue('low')
            Queues._initialized = True
