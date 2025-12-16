import django_rq

from typing import Callable


class Queue:

    def __init__(self, name: str):
        self.name = name

    def add(self, func: Callable, *args, **kwargs):
        django_rq.enqueue(self.name, func, *args, **kwargs)

    def get_all(self):
        return django_rq.get_all_queues()

    def get_queue(self):
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
