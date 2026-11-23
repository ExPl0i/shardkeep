"""Сведения о системе без сторонних пакетов: свободная оперативная память."""

import os
import sys


def available_memory():
    """Свободная оперативная память в байтах или None, если определить не удалось."""
    try:
        if os.name == "nt":
            return _windows_available()
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_AVPHYS_PAGES")
            size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and size > 0:
                return pages * size
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _windows_available():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys)
