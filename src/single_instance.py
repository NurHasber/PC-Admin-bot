from __future__ import annotations

import atexit
import ctypes
import logging
import os
from ctypes import wintypes
from pathlib import Path

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

_MUTEX_NAME = "Local\\PCAdminBot_SingleInstance_v2"
_ERROR_ALREADY_EXISTS = 183
_MB_ICONINFORMATION = 0x40
_PID_FILE = ROOT_DIR / ".pc_admin_bot.pid"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_kernel32.CreateMutexW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_user32.MessageBoxW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.UINT,
]
_user32.MessageBoxW.restype = ctypes.c_int

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_mutex_handle: wintypes.HANDLE | None = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def _read_pid_file() -> int | None:
    try:
        raw = _PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw)
    except Exception:
        return None


def _write_pid_file() -> None:
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _clear_pid_file() -> None:
    try:
        if _PID_FILE.is_file():
            existing = _read_pid_file()
            if existing is None or existing == os.getpid():
                _PID_FILE.unlink(missing_ok=True)
    except Exception:
        logger.debug("pid file cleanup ignored", exc_info=True)


def try_acquire() -> bool:
    """Return True if this process owns the single-instance lock."""
    global _mutex_handle

    stale = _read_pid_file()
    if stale is not None and stale != os.getpid() and _pid_alive(stale):
        logger.info("PID file points to live process %s", stale)
        return False

    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        logger.error("CreateMutexW failed: %s", ctypes.get_last_error())
        return False

    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        logger.info("Mutex %s already held", _MUTEX_NAME)
        return False

    _mutex_handle = handle
    _write_pid_file()
    atexit.register(release)
    return True


def notify_already_running() -> None:
    _user32.MessageBoxW(
        None,
        "PC Admin Bot уже запущен.\nВторой экземпляр не будет стартовать.\n"
        "Если это ошибка — scripts\\stop.bat, затем снова run.vbs.",
        "PC Admin Bot",
        _MB_ICONINFORMATION,
    )


def release() -> None:
    global _mutex_handle
    _clear_pid_file()
    if _mutex_handle:
        _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
