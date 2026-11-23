"""Сжатие и распаковка потоков: zstd (пакет zstandard или модуль compression.zstd) и xz."""

import lzma

from shardkeep.errors import UserError

try:
    from compression import zstd as _stdzstd  # Python 3.14+
except Exception:  # noqa: BLE001
    _stdzstd = None
try:
    import zstandard as _zstd
except Exception:  # noqa: BLE001
    _zstd = None

EXT_BY_CODEC = {"zstd": ".tar.zst", "xz": ".tar.xz"}
CODEC_BY_EXT = {".zst": "zstd", ".xz": "xz"}

# Окно 128 МБ: такие архивы распаковываются любой программой zstd без дополнительных параметров.
WINDOW_LOG = 27
# Предельный размер окна при распаковке (2 ГБ), чтобы читать и архивы, сжатые с большим окном.
MAX_WINDOW = 2**31

DEFAULT_LEVEL = {"zstd": 19, "xz": 9}


def have_zstd():
    return _zstd is not None or _stdzstd is not None


def pick_codec(requested):
    if requested in (None, "auto"):
        return "zstd" if have_zstd() else "xz"
    if requested == "zstd" and not have_zstd():
        raise UserError("Для zstd установите пакет: pip install zstandard (или используйте --codec xz)")
    if requested not in EXT_BY_CODEC:
        raise UserError(f"Неизвестный кодек: {requested}")
    return requested


def codec_of(path):
    for ext, codec in CODEC_BY_EXT.items():
        if path.endswith(ext):
            return codec
    return None


def open_writer(path, codec, level, threads, long_mode):
    """Открывает файл для потоковой записи со сжатием. Возвращает объект с write() и close()."""
    if codec == "zstd":
        if _zstd is not None:
            kw = {"threads": threads, "write_checksum": True}
            if long_mode:
                kw.update(enable_ldm=True, window_log=WINDOW_LOG)
            params = _zstd.ZstdCompressionParameters.from_level(level, **kw)
            fh = open(path, "wb")
            return _zstd.ZstdCompressor(compression_params=params).stream_writer(fh, closefd=True)
        if _stdzstd is None:
            raise UserError("Для zstd установите пакет: pip install zstandard")
        P = _stdzstd.CompressionParameter
        opts = {P.compression_level: level, P.checksum_flag: 1}
        if threads > 1:
            opts[P.nb_workers] = threads
        if long_mode:
            opts[P.enable_long_distance_matching] = 1
            opts[P.window_log] = WINDOW_LOG
        return _stdzstd.ZstdFile(path, "wb", options=opts)
    preset = min(9, max(0, level))
    if level >= 10:
        preset = 9 | lzma.PRESET_EXTREME
    return lzma.open(path, "wb", preset=preset)


def open_reader(path):
    """Открывает сжатый файл для потокового чтения по расширению."""
    if path.endswith(".zst"):
        if _zstd is not None:
            fh = open(path, "rb")
            return _zstd.ZstdDecompressor(max_window_size=MAX_WINDOW).stream_reader(
                fh, read_across_frames=True, closefd=True)
        if _stdzstd is not None:
            P = _stdzstd.DecompressionParameter
            return _stdzstd.ZstdFile(path, "rb", options={P.window_log_max: 31})
        raise UserError("Для распаковки .zst установите пакет: pip install zstandard")
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    return open(path, "rb")


class HashingReader:
    """Считает хеш файла во время чтения его архиватором."""

    def __init__(self, f, h):
        self.f = f
        self.h = h
        self.n = 0

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        self.n += len(b)
        return b


class CountingWriter:
    def __init__(self, w):
        self.w = w
        self.n = 0

    def write(self, b):
        self.n += len(b)
        return self.w.write(b)

    def flush(self):
        if hasattr(self.w, "flush"):
            self.w.flush()
