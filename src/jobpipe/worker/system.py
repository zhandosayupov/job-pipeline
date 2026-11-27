"""Being a good guest on the owner's laptop (ADR 0006): low priority, apply only on AC power and when
the owner has been away from the keyboard for a while. Windows APIs via ctypes; no-ops elsewhere."""
from __future__ import annotations

import contextlib
import os
import sys


def lower_priority() -> None:
    if sys.platform == "win32":
        import ctypes

        below_normal = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), below_normal)
    else:
        with contextlib.suppress(OSError):
            os.nice(10)


def on_ac_power() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes

    class SystemPowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    st = SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return True
    return st.ACLineStatus != 0  # 1 = online, 255 = unknown (desktops): treat as powered


def idle_seconds() -> float:
    if sys.platform != "win32":
        return 1e9
    import ctypes

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
