"""Local-host process liveness.

Extracted so the scheduler lease and saga ownership share ONE implementation. Both answer the same
question -- is the process holding this claim still running -- and two copies of a platform-specific
liveness check is how they drift apart.

**A PID is only meaningful on the host that recorded it.** A remote PID cannot be inspected locally,
and asking anyway answers about an unrelated process on this machine. Callers must compare hostnames
first; :func:`pid_alive` deliberately does not do it for them, because it cannot know which hostname
the caller stored alongside the PID.

Conservative by design: every uncertainty resolves to ALIVE. For the saga that bias is the safe one
-- treating a live writer as dead is what produces a double-apply, while treating a dead writer as
live merely delays recovery.
"""

from __future__ import annotations

import os
import socket
import sys


def hostname() -> str:
    return socket.gethostname()


def pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running ON THIS HOST.

    Used to reclaim a claim whose owner was hard-killed before its TTL expired. On Windows a restart
    terminates the old process with TerminateProcess (not a graceful signal), so its shutdown hook
    never runs and the claim is never released; on a fast restart the TTL has not expired either, so
    a successor would otherwise be blocked until expiry or a manual takeover.

    Conservative by design: any uncertainty (permission error, unexpected platform result) is treated
    as ALIVE, so a genuinely live owner is never displaced.

    Note for saga callers: a recycled PID reads as alive even though the original process is gone.
    That is the SAFE direction -- it yields "cannot prove death", not a false claim of death.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87  # PID does not exist
        # Declare signatures: a HANDLE is pointer-sized, so the default 32-bit c_int restype
        # would TRUNCATE it on Win64 and corrupt the handle. Set restype/argtypes explicitly.
        # GetExitCodeProcess (not WaitForSingleObject) is used because the query-limited access
        # right does not grant SYNCHRONIZE, which Wait* requires.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No handle: invalid-parameter => the PID does not exist (dead). Any other error
            # (e.g. access-denied) => it exists but we cannot inspect it, so treat as alive.
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # cannot read status -- do not displace
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return True  # unknown -- do not displace
    return True


__all__ = ["hostname", "pid_alive"]
