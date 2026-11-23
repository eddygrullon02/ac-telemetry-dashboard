"""
window_finder.py - finds the Assetto Corsa window on screen (Windows only)
==========================================================================

WHAT THIS DOES
    Looks through the visible windows for the game's process (acs.exe) and
    returns the position and size of its drawing area in DESKTOP coordinates.
    Desktop coordinates cover all monitors at once, so this works the same for
    a single screen, two monitors, or a triple-screen setup, and it doesn't
    matter which monitor or graphics card the game is on.

    No extra packages needed: it talks to Windows directly through ctypes.

USED BY
    camera.py (to capture only the game window)
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Report real pixels instead of "scaled" ones on high-DPI displays. If another
# library already set this, Windows refuses the second call, which is fine.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor DPI aware
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

# Process names of the game. The 3D window belongs to acs.exe.
GAME_EXES = {"acs.exe", "acs_x86.exe"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Declaring argument types stops 64-bit handles from being cut down to 32 bits.
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _process_name(pid):
    """Return the lowercase exe name for a process id, e.g. 'acs.exe' ('' if unknown)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _client_rect(hwnd):
    """Drawing area of a window (no title bar or borders) in desktop coordinates."""
    rc = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(0, 0)                     # client (0, 0) -> desktop position
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    return {"left": pt.x, "top": pt.y, "width": w, "height": h}


def _visible_windows():
    """Yield (hwnd, exe_name) for every visible, non-minimised window."""
    found = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd, _process_name(pid.value)))
        return True

    proc = WNDENUMPROC(callback)                  # keep a reference while Windows calls it
    user32.EnumWindows(proc, 0)
    return found


def find_game_region(exes=GAME_EXES):
    """Return {'left','top','width','height'} of the game window, or None if not found.

    If the game has several windows, the largest one is used.
    """
    best = None
    for hwnd, exe in _visible_windows():
        if exe in exes:
            rect = _client_rect(hwnd)
            if rect and (best is None or
                         rect["width"] * rect["height"] > best["width"] * best["height"]):
                best = rect
    return best


def describe_visible_windows():
    """Diagnostic: list visible windows with their exe name, title and size."""
    rows = []
    for hwnd, exe in _visible_windows():
        rect = _client_rect(hwnd)
        if rect and rect["width"] > 100 and rect["height"] > 100:
            rows.append((exe or "?", _title(hwnd), rect))
    return rows
