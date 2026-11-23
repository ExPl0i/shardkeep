"""shardkeep compare-zip: повторяет ли zip-архив содержимое распакованной папки (только чтение).

Файлы архива сопоставляются с файлами папки по имени и пути (начало пути внутри архива может отличаться,
например архив содержит папку верхнего уровня). Затем сравниваются размеры и, с параметром crc,
контрольные суммы CRC32: zip хранит CRC32 каждого файла, поэтому архив распаковывать не нужно,
читается только папка. Параметр test распаковывает архив в память и проверяет его целостность.
"""

import os
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field

from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path
from shardkeep.progress import CallbackReader, Progress, echo
from shardkeep.textfmt import human, num


@dataclass
class ZipCompareResult:
    matched: int = 0
    size_mismatch: list = field(default_factory=list)
    crc_mismatch: list = field(default_factory=list)
    only_in_zip: list = field(default_factory=list)
    only_in_folder: list = field(default_factory=list)
    damaged_member: object = None
    tested: bool = False

    @property
    def complete(self):
        """Архив содержит все файлы папки с теми же размерами (и CRC32, если они проверялись)."""
        return not (self.size_mismatch or self.crc_mismatch or self.only_in_folder or self.damaged_member)


def crc32_file(path, block=8 << 20, on_read=None):
    c = 0
    with open(path, "rb") as raw:
        f = CallbackReader(raw, on_read) if on_read else raw
        for b in iter(lambda: f.read(block), b""):
            c = zlib.crc32(b, c)
    return c & 0xFFFFFFFF


def test_members(z, entries):
    """Распаковывает каждый файл архива в память и проверяет CRC32, как ZipFile.testzip, но с прогрессом.
    Возвращает имя первого поврежденного файла или None."""
    with Progress("zip test", total=sum(zi.file_size for zi in entries)) as bar:
        for zi in entries:
            try:
                with z.open(zi) as f:
                    for b in iter(lambda: f.read(8 << 20), b""):
                        bar.update(len(b))
            except (zipfile.BadZipFile, zlib.error, OSError, EOFError, NotImplementedError):
                return zi.filename
    return None


def list_folder(folder):
    with Progress("scanning", unit="files") as bar:
        return _list_folder(folder, bar)


def _list_folder(folder, bar):
    root = long_path(folder)
    out = {}
    stack = [(root, "")]
    while stack:
        p, rel = stack.pop()
        try:
            entries = list(os.scandir(p))
        except OSError as exc:
            echo(f"  skipped {rel or '.'}: {exc}")
            continue
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, r))
                elif e.is_file(follow_symlinks=False):
                    out[r] = e.stat(follow_symlinks=False).st_size
                    bar.update()
            except OSError as exc:
                echo(f"  skipped {r}: {exc}")
    return out


def match_entries(entries, local):
    """Сопоставляет файлы архива и папки. Возвращает ([(путь в папке, ZipInfo)], [имена только в архиве])."""
    by_name = defaultdict(list)
    for r in local:
        by_name[r.rsplit("/", 1)[-1]].append(r)
    pairs, only_zip, used = [], [], set()
    for zi in entries:
        zname = zi.filename.replace("\\", "/")
        cands = [c for c in by_name.get(zname.rsplit("/", 1)[-1], []) if c not in used]
        pick = None
        for c in cands:
            if zname == c or zname.endswith("/" + c) or c.endswith("/" + zname):
                pick = c
                break
        if pick is None and len(cands) == 1:
            pick = cands[0]
        if pick is None:
            only_zip.append(zname)
            continue
        used.add(pick)
        pairs.append((pick, zi))
    return pairs, only_zip


def compare_zip(zip_path, folder, crc=False, test=False):
    if not os.path.isfile(zip_path):
        raise UserError(f"Zip archive not found: {zip_path}")
    if not os.path.isdir(folder):
        raise UserError(f"Folder not found: {folder}")
    local = list_folder(folder)
    res = ZipCompareResult(tested=test)
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise UserError(f"Not a valid zip archive: {zip_path} ({exc})") from exc
    with z:
        entries = [zi for zi in z.infolist() if not zi.is_dir()]
        print(f"Archive: {num(len(entries))} files, {human(sum(zi.file_size for zi in entries))} uncompressed, "
              f"{human(os.path.getsize(zip_path))} on disk")
        print(f"Folder: {num(len(local))} files, {human(sum(local.values()))}")
        pairs, res.only_in_zip = match_entries(entries, local)
        same_size = []
        for pick, zi in pairs:
            if local[pick] != zi.file_size:
                res.size_mismatch.append(pick)
            else:
                same_size.append((pick, zi))
        if crc:
            root = long_path(folder)
            with Progress("CRC32", total=sum(local[p] for p, _ in same_size)) as bar:
                for p, zi in same_size:
                    if crc32_file(os_path(root, p), on_read=bar.update) != zi.CRC:
                        res.crc_mismatch.append(p)
        if test:
            print("Testing archive integrity ...")
            res.damaged_member = test_members(z, entries)
    used = {p for p, _ in pairs}
    res.only_in_folder = sorted(r for r in local if r not in used)
    res.matched = len(same_size) - len(res.crc_mismatch)

    print(f"\nMatching by {'size and CRC32' if crc else 'size'}: {num(res.matched)}")
    for title, items in (("Different size", res.size_mismatch), ("Different CRC32", res.crc_mismatch),
                         ("Only in the archive", res.only_in_zip), ("Only in the folder", res.only_in_folder)):
        if items:
            extra = f", {human(sum(local[i] for i in items))}" if title == "Only in the folder" else ""
            print(f"{title}: {num(len(items))}{extra}")
            for i in items[:10]:
                print(f"  {i}")
    if test:
        print("The archive is intact." if res.damaged_member is None
              else f"The archive is damaged, first bad file: {res.damaged_member}")
    if res.complete:
        how = ("sizes and checksums match" if crc
               else "names and sizes match (run with --crc to compare checksums too)")
        print(f"\nResult: the archive contains every file of the folder, {how}.")
    else:
        print("\nResult: the archive does NOT fully match the folder.")
    return res
