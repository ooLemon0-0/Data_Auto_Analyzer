"""Optional, thread-local progress events shared by sources and desktop jobs."""
from contextlib import contextmanager
from contextvars import ContextVar
from app.core.cancellation import cancellation_scope, check_cancelled


_callback = ContextVar("operation_progress", default=None)
_collection_cache = ContextVar("collection_cache", default=None)


@contextmanager
def progress_scope(callback, *, collection_cache=None, cancellation_token=None):
    token = _callback.set(callback)
    cache_token = _collection_cache.set(collection_cache)
    try:
        with cancellation_scope(cancellation_token):
            yield
    finally:
        _callback.reset(token)
        _collection_cache.reset(cache_token)


def shared_collection_cache():
    """Only shared by jobs explicitly started in the same desktop batch."""
    return _collection_cache.get()


def report_progress(stage: str, message: str, current=None, total=None):
    check_cancelled()
    callback = _callback.get()
    if callback:
        callback({"stage": stage, "message": message, "current": current, "total": total})
