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


class HashingFile:
    """Обертка над файлом: считает хеш записанных или прочитанных байтов."""

    def __init__(self, f, h=None):
        self.f = f
        self.h = h
        self.n = 0

    def write(self, b):
        if self.h is not None:
            self.h.update(b)
        self.n += len(b)
        return self.f.write(b)

    def read(self, n=-1):
        b = self.f.read(n)
        if self.h is not None:
            self.h.update(b)
        self.n += len(b)
        return b

    def readinto(self, buf):
        b = self.read(len(buf))
        buf[: len(b)] = b
        return len(b)

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return False

    def flush(self):
        self.f.flush()

    def drain(self):
        """Дочитывает файл до конца, чтобы хеш охватил все байты."""
        while self.read(8 << 20):
            pass

    @property
    def closed(self):
        return self.f.closed

    def close(self):
        if not self.f.closed:
            self.f.close()


class Stream:
    """Сжимающий или распаковывающий поток вместе с файлом под ним."""

    def __init__(self, s, raw):
        self.s = s
        self.raw = raw

    def write(self, b):
        return self.s.write(b)

    def read(self, n=-1):
        return self.s.read(n)

    def flush(self):
        if hasattr(self.s, "flush"):
            self.s.flush()

    def close(self):
        try:
            self.s.close()
        finally:
            self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_writer(path, codec, level, threads, long_mode, hasher=None):
    """Открывает файл для потоковой записи со сжатием.

    hasher (необязательно) получает все сжатые байты, записанные в файл.
    """
    raw = HashingFile(open(path, "wb"), hasher)
    try:
        if codec == "zstd":
            if _zstd is not None:
                kw = {"threads": threads if threads > 1 else 0, "write_checksum": True}
                if long_mode:
                    kw.update(enable_ldm=True, window_log=WINDOW_LOG)
                params = _zstd.ZstdCompressionParameters.from_level(level, **kw)
                return Stream(_zstd.ZstdCompressor(compression_params=params).stream_writer(raw, closefd=False),
                              raw)
            if _stdzstd is None:
                raise UserError("Для zstd установите пакет: pip install zstandard")
            P = _stdzstd.CompressionParameter
            opts = {P.compression_level: level, P.checksum_flag: 1}
            if threads > 1:
                opts[P.nb_workers] = threads
            if long_mode:
                opts[P.enable_long_distance_matching] = 1
                opts[P.window_log] = WINDOW_LOG
            return Stream(_stdzstd.ZstdFile(raw, "wb", options=opts), raw)
        preset = min(9, max(0, level))
        if level >= 10:
            preset = 9 | lzma.PRESET_EXTREME
        return Stream(lzma.open(raw, "wb", preset=preset), raw)
    except BaseException:
        raw.close()
        raise


def open_reader(path, hasher=None):
    """Открывает сжатый файл для потокового чтения по расширению.

    hasher (необязательно) получает все байты файла; для полного хеша вызовите drain_raw().
    """
    raw = HashingFile(open(path, "rb"), hasher)
    try:
        if path.endswith(".zst"):
            if _zstd is not None:
                s = _zstd.ZstdDecompressor(max_window_size=MAX_WINDOW).stream_reader(
                    raw, read_across_frames=True, closefd=False)
            elif _stdzstd is not None:
                P = _stdzstd.DecompressionParameter
                s = _stdzstd.ZstdFile(raw, "rb", options={P.window_log_max: 31})
            else:
                raise UserError("Для распаковки .zst установите пакет: pip install zstandard")
        elif path.endswith(".xz"):
            s = lzma.open(raw, "rb")
        else:
            s = raw
        return Stream(s, raw)
    except BaseException:
        raw.close()
        raise


def drain(stream):
    """Дочитывает распакованный поток и файл под ним до конца."""
    while stream.read(8 << 20):
        pass
    stream.raw.drain()


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
    """Считает записанные байты. После abort() запись молча отбрасывается: так tar, брошенный
    на середине, не пытается дописать данные в уже закрытый поток."""

    def __init__(self, w):
        self.w = w
        self.n = 0
        self.aborted = False

    def abort(self):
        self.aborted = True

    def write(self, b):
        if self.aborted:
            return len(b)
        self.n += len(b)
        return self.w.write(b)

    def flush(self):
        if hasattr(self.w, "flush"):
            self.w.flush()
