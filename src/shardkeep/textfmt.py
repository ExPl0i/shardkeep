"""Formatting of numbers, sizes and durations for program messages."""


def human(n):
    """Size in bytes as "1.5 GB"."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def num(n):
    """Integer with thousands separators: 1,234,567."""
    return f"{n:,}"


def pct(part, total):
    return f"{100 * part / total if total else 0:.1f}%"


def fmt_time(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600} h {sec % 3600 // 60:02d} min"
    return f"{sec // 60} min {sec % 60:02d} s"


def plural(n, forms):
    """plural(5, ("file", "files")) -> "files"."""
    return forms[0] if abs(int(n)) == 1 else forms[1]


FILES = ("file", "files")
