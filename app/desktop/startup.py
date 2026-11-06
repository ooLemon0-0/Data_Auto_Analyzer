"""Startup status without importing Qt or application settings."""
import ctypes
import os

_callback = None


def set_startup_callback(callback):
    global _callback
    _callback = callback


def report_startup(message, current=None, total=None):
    if _callback:
        _callback(message, current, total)


def notify_window_visible():
    name = os.environ.pop("AUTOANALYZER_VISIBLE_EVENT", "")
    if not name or os.name != "nt":
        return
    kernel = ctypes.windll.kernel32
    kernel.OpenEventW.restype = ctypes.c_void_p
    handle = kernel.OpenEventW(0x0002, False, name)
    if handle:
        kernel.SetEvent(ctypes.c_void_p(handle))
        kernel.CloseHandle(ctypes.c_void_p(handle))
