"""Профили сжатия, выбор уровня по содержимому и число потоков по доступной памяти.

Хорошо сжимаемые данные (текст, JSON, CSV, массивы без сжатия) сжимаются высоким уровнем.
Уже сжатые форматы (JPEG, PNG, MP4, zip и подобные) высокий уровень почти не уменьшает,
а время тратит в десятки раз больше. Такие файлы собираются в отдельные части и сжимаются
низким уровнем.
"""

import os
import random
from collections import defaultdict
from dataclasses import dataclass

from shardkeep import codecs
from shardkeep.fsutil import os_path
from shardkeep.sysinfo import available_memory
from shardkeep.textfmt import human, pct

HIGH, LOW = "high", "low"

# Форматы, которые уже сжаты внутри.
INCOMPRESSIBLE_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".heif", ".jxl", ".gif",
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".mpg", ".mpeg", ".ts",
    ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".flac", ".wma",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".lz4", ".7z", ".rar", ".br", ".lzma",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".epub", ".jar", ".apk", ".whl",
}

LOW_RATIO = 0.90  # если пробное сжатие уменьшает данные меньше чем на 10 %, они считаются несжимаемыми


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    zstd_high: int
    zstd_low: object   # None – один уровень для всех файлов
    xz_high: int
    xz_low: object
    long: bool
    classify: object   # None, "ext" (по расширению) или "trial" (пробное сжатие выборки)

    def levels(self, codec):
        if codec == "xz":
            return self.xz_high, self.xz_low
        return self.zstd_high, self.zstd_low


PROFILES = {
    "auto": Profile("auto", "уровень по пробному сжатию выборки: сжимаемые файлы – zstd 19, уже сжатые – zstd 1",
                    19, 1, 9, 0, True, "trial"),
    "max": Profile("max", "максимальное сжатие всех файлов: zstd 22, медленно", 22, None, 10, None, True, None),
    "normal": Profile("normal", "zstd 19 для всех файлов (как в исходном pack_shards.py)", 19, None, 9, None, True,
                      None),
    "fast": Profile("fast", "быстрое сжатие: zstd 3 без режима длинных совпадений", 3, None, 3, None, False, None),
    "media": Profile("media", "уровень по расширению: фото, видео и архивы – zstd 1, остальное – zstd 19",
                     19, 1, 9, 0, True, "ext"),
}
DEFAULT_PROFILE = "auto"


def ext_of(path):
    return os.path.splitext(path)[1].lower()


def classify_extensions(src_os, files, method, sample_files=6, chunk=1 << 20, min_bytes=256 << 10):
    """Делит расширения на хорошо сжимаемые (HIGH) и уже сжатые (LOW).

    files – список (путь, размер, время). Возвращает ({расширение: класс}, {расширение: (байт, доля, способ)}).
    """
    groups = defaultdict(list)
    for rel, size, _ in files:
        groups[ext_of(rel)].append((rel, size))
    classes, stats = {}, {}
    rng = random.Random(0)
    for ext in sorted(groups):
        items = groups[ext]
        total = sum(s for _, s in items)
        by_table = LOW if ext in INCOMPRESSIBLE_EXT else HIGH
        if method != "trial" or total < min_bytes:
            classes[ext] = by_table
            stats[ext] = (total, None, "по расширению")
            continue
        sample = [it for it in sorted(items) if it[1] > 0]
        if len(sample) > sample_files:
            sample = rng.sample(sample, sample_files)
        ratio = _trial_ratio(src_os, sample, chunk)
        if ratio is None:
            classes[ext] = by_table
            stats[ext] = (total, None, "по расширению")
        else:
            classes[ext] = LOW if ratio > LOW_RATIO else HIGH
            stats[ext] = (total, ratio, "пробное сжатие")
    return classes, stats


def _trial_ratio(src_os, sample, chunk):
    buf = bytearray()
    for rel, size in sample:
        try:
            with open(os_path(src_os, rel), "rb") as f:
                if size > chunk:
                    f.seek((size - chunk) // 2)
                buf += f.read(chunk)
        except OSError:
            continue
    if not buf:
        return None
    return len(_quick_compress(bytes(buf))) / len(buf)


def _quick_compress(data):
    if codecs._zstd is not None:
        return codecs._zstd.ZstdCompressor(level=3).compress(data)
    if codecs._stdzstd is not None:
        return codecs._stdzstd.compress(data, level=3)
    import lzma
    return lzma.compress(data, preset=1)


def describe_classes(stats, classes):
    """Строки отчета о том, какие файлы будут сжаты низким уровнем."""
    low = [(e, stats[e]) for e in classes if classes[e] == LOW and stats[e][0] > 0]
    if not low:
        return []
    total = sum(st[0] for st in stats.values())
    low_total = sum(st[0] for _, st in low)
    parts = []
    for e, (b, ratio, _) in sorted(low, key=lambda x: -x[1][0])[:8]:
        r = f", пробное сжатие до {pct(ratio, 1)}" if ratio is not None else ""
        parts.append(f"{e or '(без расширения)'} {human(b)}{r}")
    return [f"Уже сжатые файлы: {human(low_total)} ({pct(low_total, total)} объема) – " + "; ".join(parts)]


# ----------------------------------------------------------------------------
# Память и потоки
# ----------------------------------------------------------------------------
JOB_MAX = 512 << 20
RESERVE = 1 << 30  # оставить системе и другим программам


def zstd_window_log(level, long_mode):
    if long_mode:
        return codecs.WINDOW_LOG
    if codecs._zstd is not None:
        return codecs._zstd.ZstdCompressionParameters.from_level(level).window_log
    return 19 if level <= 3 else 22 if level <= 12 else 23


def zstd_memory_per_thread(level, long_mode, threads):
    """Примерный объем памяти на один поток сжатия zstd, байт.

    В многопоточном режиме каждый поток держит свой контекст сжатия, входной и выходной буферы задания
    (до 512 МБ каждый при окне 128 МБ) и копию окна.
    """
    wlog = zstd_window_log(level, long_mode)
    window = 1 << wlog
    ctx = None
    if codecs._zstd is not None:
        try:
            # Оценка без режима длинных совпадений: с ним zstandard 0.25 аварийно завершает процесс.
            p = codecs._zstd.ZstdCompressionParameters.from_level(level, window_log=wlog)
            ctx = p.estimated_compression_context_size()
        except Exception:  # noqa: BLE001
            ctx = None
    if ctx is None:
        ctx = window + (32 << 20 if level <= 3 else 128 << 20 if level <= 12 else 640 << 20)
    if long_mode:
        ctx += (1 << max(wlog - 7, 0)) * 8  # таблица поиска длинных совпадений
    if threads <= 1:
        return ctx
    job = min(1 << (wlog + 2), JOB_MAX)
    return ctx + 2 * job + window


def choose_threads(codec, level, long_mode, requested=0, avail=None):
    """Число потоков сжатия и пояснение, если оно ограничено памятью."""
    if codec != "zstd":
        return 1, None
    cpu = os.cpu_count() or 1
    want = requested or cpu
    avail = available_memory() if avail is None else avail
    if not avail or want <= 1:
        return want, None
    per = zstd_memory_per_thread(level, long_mode, want)
    fit = max(1, int(max(avail - RESERVE, 0) // per))
    if want <= fit:
        return want, None
    note = (f"свободно {human(avail)} памяти, одному потоку zstd уровня {level} нужно примерно {human(per)}")
    if requested:
        return want, f"Внимание: потоков {want} может не хватить памяти ({note}). Рекомендуется не больше {fit}."
    return fit, f"Потоков {fit} вместо {cpu}: {note}."
