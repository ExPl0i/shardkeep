"""Форматирование чисел, объемов и времени для сообщений на русском."""


def human(n):
    """Объем в байтах в виде «1,5 ГБ»."""
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{int(n)} Б" if unit == "Б" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


def num(n):
    """Целое число с пробелами между разрядами: 1 234 567."""
    return f"{n:,}".replace(",", " ")


def pct(part, total):
    return f"{100 * part / total if total else 0:.1f} %".replace(".", ",")


def fmt_time(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60:02d} мин"
    return f"{sec // 60} мин {sec % 60:02d} с"


def plural(n, forms):
    """Форма слова для числа: plural(5, ("файл", "файла", "файлов")) -> "файлов"."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


FILES = ("файл", "файла", "файлов")
