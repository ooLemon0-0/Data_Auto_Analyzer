"""Cooperative task cancellation; cached files and committed audits are retained."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, RLock
import socket
import time


class WorkCancelled(Exception):
    pass


class CancellationToken:
    def __init__(self):
        self.event = Event()
        self.lock = RLock()
        self.sockets = []

    @property
    def cancelled(self):
        return self.event.is_set()

    def check(self):
        if self.cancelled:
            raise WorkCancelled("任务已停止，已完成缓存和已保存审核结果保留")

    def register_socket(self, connection):
        with self.lock:
            self.sockets.append(connection)
            cancelled = self.cancelled
        if cancelled:
            self._close_socket(connection)
            self.check()

    @staticmethod
    def _close_socket(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass
        # Response.makefile() owns another Python I/O reference. socket.close()
        # alone can defer closing the Windows handle until a blocked read ends.
        # Detach first, so later cleanup cannot close a recycled descriptor.
        try:
            descriptor = connection.detach()
            if descriptor >= 0:
                socket.close(descriptor)
        except (OSError, ValueError):
            pass

    def cancel(self):
        self.event.set()
        with self.lock:
            connections, self.sockets = self.sockets, []
        for connection in connections:
            self._close_socket(connection)


_token = ContextVar("cancellation_token", default=None)


def current_token():
    return _token.get()


@contextmanager
def cancellation_scope(token):
    marker = _token.set(token)
    try:
        yield
    finally:
        _token.reset(marker)


def check_cancelled():
    token = current_token()
    if token:
        token.check()


def cancel_pause(seconds):
    token = current_token()
    if token:
        if token.event.wait(seconds):
            token.check()
    else:
        time.sleep(seconds)


@contextmanager
def cancellable_lock(lock):
    while not lock.acquire(timeout=0.1):
        check_cancelled()
    try:
        check_cancelled()
        yield
    finally:
        lock.release()
