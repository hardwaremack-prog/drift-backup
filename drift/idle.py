"""
Cross-platform "how long has this computer been idle" detection.

Real idle time (no mouse/keyboard input) is OS-specific and not available
through any single portable API. We try the real thing per-platform, and
fall back to a CPU-load heuristic if it's unavailable (e.g. no display,
missing OS helper, sandboxed environment). The fallback is clearly marked
so `drift status` can tell you which one is in effect.
"""

from __future__ import annotations
import platform
import shutil
import subprocess
import time

import psutil

_SYSTEM = platform.system()


def _idle_seconds_macos() -> float | None:
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"], stderr=subprocess.DEVNULL, text=True, timeout=2
        )
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000
    except Exception:
        return None
    return None


def _idle_seconds_windows() -> float | None:
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):  # type: ignore
            return None
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime  # type: ignore
        return millis / 1000.0
    except Exception:
        return None


def _idle_seconds_linux() -> float | None:
    # Prefer xprintidle if the user has it installed (X11 sessions).
    if shutil.which("xprintidle"):
        try:
            out = subprocess.check_output(["xprintidle"], stderr=subprocess.DEVNULL, timeout=2)
            return int(out.strip()) / 1000.0
        except Exception:
            pass
    # Try the freedesktop screensaver DBus interface (works on many desktops,
    # including some Wayland compositors that implement it).
    if shutil.which("dbus-send") or shutil.which("busctl"):
        try:
            out = subprocess.check_output(
                ["busctl", "--user", "call", "org.freedesktop.ScreenSaver",
                 "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver",
                 "GetSessionIdleTime"],
                stderr=subprocess.DEVNULL, text=True, timeout=2,
            )
            digits = "".join(ch for ch in out.split()[-1] if ch.isdigit())
            if digits:
                return int(digits) / 1000.0
        except Exception:
            pass
    return None


_PLATFORM_FN = {
    "Darwin": _idle_seconds_macos,
    "Windows": _idle_seconds_windows,
    "Linux": _idle_seconds_linux,
}


class IdleMonitor:
    """
    Tracks whether the machine looks "at rest": genuinely idle (no input)
    if the OS gives us that, otherwise a conservative CPU-load proxy.
    """

    def __init__(self):
        self._fn = _PLATFORM_FN.get(_SYSTEM)
        self._using_real_idle = self._fn is not None and self._fn() is not None
        self._cpu_history: list[float] = []
        self._busy_since: float | None = None

    @property
    def method(self) -> str:
        return "input-idle-time" if self._using_real_idle else "cpu-load-heuristic"

    def idle_seconds(self) -> float:
        if self._using_real_idle:
            val = self._fn()
            if val is not None:
                return val
            self._using_real_idle = False  # degrade gracefully mid-run

        # Fallback: treat "idle" as "CPU load has been low for a while".
        cpu = psutil.cpu_percent(interval=0.3)
        self._cpu_history.append(cpu)
        self._cpu_history = self._cpu_history[-40:]
        now = time.time()
        if cpu > 20:
            self._busy_since = now
            return 0.0
        if self._busy_since is None:
            self._busy_since = now - 1
        return now - self._busy_since

    def battery_ok(self) -> bool:
        """False only if we're clearly on battery and getting low."""
        try:
            b = psutil.sensors_battery()
        except Exception:
            b = None
        if b is None:
            return True  # desktop / no battery info: don't block on it
        if b.power_plugged:
            return True
        return b.percent > 25

    def snapshot(self) -> dict:
        return {
            "idle_seconds": round(self.idle_seconds(), 1),
            "cpu_percent": psutil.cpu_percent(interval=0.0),
            "method": self.method,
            "battery_ok": self.battery_ok(),
        }
