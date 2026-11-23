"""Необязательный перевод JPEG в JPEG XL без потерь с побайтным восстановлением исходного файла.

Используются программы cjxl и djxl из libjxl (https://github.com/libjxl/libjxl). cjxl с параметром
--lossless_jpeg=1 сохраняет в файле .jxl все данные для восстановления исходного JPEG, а djxl
восстанавливает его байт в байт. Обычно файл уменьшается примерно на 20 %.

При упаковке каждый файл сразу восстанавливается обратно и сравнивается с исходным по SHA-256.
Если перевод не удался, файл не восстановился побайтно или .jxl получился больше исходного,
в архив кладется исходный JPEG.

Программы вызываются для временных копий с короткими именами из латинских букв: так не мешают
кириллица и длинные пути Windows.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from shardkeep.errors import UserError
from shardkeep.fsutil import os_path

JPEG_EXT = {".jpg", ".jpeg"}
MEMBER_SUFFIX = ".jxl"
INSTALL_HINT = ("JPEG XL needs the cjxl and djxl programs from libjxl: https://github.com/libjxl/libjxl/releases "
                "(Windows: winget install libjxl, or the release archive; Linux: apt install libjxl-tools; "
                "macOS: brew install jpeg-xl)")


def find_tools():
    """Команды cjxl и djxl в виде списков аргументов или None. Тесты подменяют эту функцию."""
    c, d = shutil.which("cjxl"), shutil.which("djxl")
    return ([c] if c else None), ([d] if d else None)


def require_tools(need_encoder=True):
    cjxl, djxl = find_tools()
    if djxl is None or (need_encoder and cjxl is None):
        raise UserError(INSTALL_HINT)
    return cjxl, djxl


def tool_version(cmd):
    try:
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip().splitlines()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def is_jpeg(rel):
    return os.path.splitext(rel)[1].lower() in JPEG_EXT


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0


def encode(cjxl, src, dst):
    return _run(cjxl + [src, dst, "--lossless_jpeg=1"]) and os.path.exists(dst)


def decode(djxl, src, dst):
    return _run(djxl + [src, dst]) and os.path.exists(dst)


class Converted:
    __slots__ = ("rel", "jxl_path", "size", "mtime", "orig_size", "orig_sha256")

    def __init__(self, rel, jxl_path, size, mtime, orig_size, orig_sha256):
        self.rel, self.jxl_path, self.size, self.mtime = rel, jxl_path, size, mtime
        self.orig_size, self.orig_sha256 = orig_size, orig_sha256


def _convert_one(tools, src_os, rel, tmpdir, k):
    """Переводит один файл. Возвращает (Converted или None, причина отказа или None)."""
    cjxl, djxl = tools
    src = os_path(src_os, rel)
    tin = os.path.join(tmpdir, f"in_{k}.jpg")
    tout = os.path.join(tmpdir, f"f_{k}.jxl")
    tback = os.path.join(tmpdir, f"back_{k}.jpg")
    try:
        shutil.copyfile(src, tin)
        st = os.stat(src)
        orig_sha = _sha256(tin)
        if not encode(cjxl, tin, tout):
            return None, "cjxl failed"
        if not decode(djxl, tout, tback):
            return None, "djxl failed"
        if _sha256(tback) != orig_sha:
            return None, "not restored byte for byte"
        size = os.path.getsize(tout)
        if size >= os.path.getsize(tin):
            return None, "JPEG XL is not smaller"
        return Converted(rel, tout, size, int(st.st_mtime), os.path.getsize(tin), orig_sha), None
    except OSError as exc:
        return None, str(exc)
    finally:
        for p in (tin, tback):
            try:
                os.remove(p)
            except OSError:
                pass


class Batch:
    """Перевод JPEG одной части во временную папку (параллельно) с последующей уборкой."""

    def __init__(self, tools, src_os, rels, workdir, workers=None, on_file=None):
        """on_file(размер) вызывается для каждого переведенного файла (для строки прогресса)."""
        self.tmpdir = tempfile.mkdtemp(prefix="sk_jxl_", dir=workdir)
        self.done = {}
        self.fallback = {}
        workers = workers or min(32, os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_convert_one, tools, src_os, rel, self.tmpdir, k): rel
                       for k, rel in enumerate(rels)}
            for fut in as_completed(futures):
                rel = futures[fut]
                conv, reason = fut.result()
                if conv is not None:
                    self.done[rel] = conv
                    if on_file is not None:
                        on_file(conv.orig_size)  # переведенный JPEG дальше в прогрессе не считается
                else:
                    self.fallback[rel] = reason

    def close(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def member_map(info):
    """{имя в архиве: исходный путь} для файлов части, переведенных в JPEG XL."""
    return {v["member"]: k for k, v in ((info or {}).get("jxl") or {}).items()}


def restore_member(djxl, fileobj, workdir):
    """Восстанавливает исходный JPEG из содержимого .jxl. Возвращает путь временного файла JPEG."""
    tmpdir = tempfile.mkdtemp(prefix="sk_djxl_", dir=workdir)
    src = os.path.join(tmpdir, "f.jxl")
    dst = os.path.join(tmpdir, "f.jpg")
    with open(src, "wb") as f:
        shutil.copyfileobj(fileobj, f, 8 << 20)
    if not decode(djxl, src, dst):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UserError("djxl could not decode a JPEG XL file from the archive")
    os.remove(src)
    return dst, tmpdir
