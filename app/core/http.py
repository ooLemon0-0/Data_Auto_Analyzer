"""Requests transport whose in-flight sockets can be closed by a task token."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Timeout

from app.core.cancellation import check_cancelled, current_token


def _attach_cancellation(manager, token):
    if getattr(manager, "_task_token", None) is token:
        return
    manager._task_token = token
    classes = dict(manager.pool_classes_by_scheme)
    for scheme, pool_class in classes.items():
        base = pool_class.ConnectionCls
        class Connection(base):
            def _new_conn(self):
                token.check()
                connection = super()._new_conn()
                token.register_socket(connection)
                return connection

            def connect(self):
                token.check()
                super().connect()
                if self.sock is not None:
                    token.register_socket(self.sock)
                token.check()
        classes[scheme] = type("TaskConnectionPool", (pool_class,), {"ConnectionCls": Connection})
    manager.pool_classes_by_scheme = classes


class TaskHTTPAdapter(HTTPAdapter):
    def __init__(self, token):
        self.token = token
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        _attach_cancellation(self.poolmanager, self.token)

    def proxy_manager_for(self, *args, **kwargs):
        manager = super().proxy_manager_for(*args, **kwargs)
        _attach_cancellation(manager, self.token)
        return manager

    def send(self, request, stream=False, timeout=None, **kwargs):
        self.token.check()
        if isinstance(timeout, tuple):
            connect, read = timeout
        elif isinstance(timeout, Timeout):
            connect, read = timeout.connect_timeout, timeout.read_timeout
        else:
            connect = read = timeout
        connect = min(float(connect), 3.0) if isinstance(connect, (int, float)) else 3.0
        try:
            response = super().send(request, stream=stream, timeout=(connect, read), **kwargs)
            self.token.check()
            return response
        except Exception:
            self.token.check()
            raise


def http_session():
    check_cancelled()
    session = requests.Session()
    token = current_token()
    if token:
        session.mount("http://", TaskHTTPAdapter(token))
        session.mount("https://", TaskHTTPAdapter(token))
    return session


def http_get(*args, **kwargs):
    token = current_token()
    if token is None:
        return requests.get(*args, **kwargs)
    try:
        with http_session() as session:
            response = session.get(*args, **kwargs)
            token.check()
            return response
    except Exception:
        token.check()
        raise
