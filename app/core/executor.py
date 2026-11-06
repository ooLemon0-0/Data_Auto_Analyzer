"""A bounded daemon worker pool that never holds application exit hostage.

Unlike ThreadPoolExecutor, this pool has no interpreter-exit join hook. Jobs use
cancellation tokens for normal cleanup; an unresponsive third-party request is
not allowed to keep the desktop process alive after the user exits.
"""
from concurrent.futures import Future
from queue import Empty, Queue
from threading import RLock, Thread, current_thread


class DaemonExecutor:
    def __init__(self, max_workers, thread_name_prefix="worker"):
        self.queue, self.lock, self.closed = Queue(), RLock(), False
        self.threads = []
        for index in range(max_workers):
            thread = Thread(target=self._worker, daemon=True, name=f"{thread_name_prefix}-{index}")
            self.threads.append(thread)
            thread.start()

    def submit(self, function, *args, **kwargs):
        with self.lock:
            if self.closed:
                raise RuntimeError("Worker pool is closed")
            future = Future()
            self.queue.put((future, function, args, kwargs))
            return future

    def _worker(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            future, function, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self.lock:
            if not self.closed:
                self.closed = True
                if cancel_futures:
                    try:
                        while True:
                            item = self.queue.get_nowait()
                            if item is not None:
                                if item[0].cancel():
                                    item[0].set_running_or_notify_cancel()
                    except Empty:
                        pass
                for _thread in self.threads:
                    self.queue.put(None)
        if wait:
            for thread in self.threads:
                if thread is not current_thread():
                    thread.join()

    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _trace):
        self.shutdown(wait=True, cancel_futures=kind is not None)
