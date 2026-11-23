"""Распаковка архивов целиком или по шаблону."""

import fnmatch
import os
import shutil
import tarfile

from shardkeep import jxl, layout
from shardkeep.codecs import open_reader
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path
from shardkeep.progress import Progress, echo
from shardkeep.textfmt import num


def _safe_name(name):
    return not (name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                or (len(name) > 1 and name[1] == ":"))


def extract_member(tar, m, target_os):
    try:
        tar.extract(m, target_os, filter="data")
    except TypeError:  # Python без параметра filter
        if not _safe_name(m.name):
            return False
        tar.extract(m, target_os)
    return True


def _restore_jpeg(djxl, tar, m, orig, exp, target_os):
    """Восстанавливает исходный JPEG из .jxl в архиве и проверяет его по манифесту."""
    if not _safe_name(orig):
        return False
    tmp, tmpdir = jxl.restore_member(djxl, tar.extractfile(m), target_os)
    try:
        if exp and (os.path.getsize(tmp) != exp["size"] or jxl._sha256(tmp) != exp["sha256"]):
            raise UserError(f"JPEG restored from {m.name} differs from the original (SHA-256 mismatch)")
        dst = os_path(target_os, orig)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(tmp, dst)
        os.utime(dst, (m.mtime, m.mtime))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return True


def unpack(out, dest, name=None, patterns=None, flat=False, keep_jxl=False):
    """Распаковывает наборы. Файлы, переведенные в JPEG XL, восстанавливаются в исходные JPEG (нужен djxl);
    keep_jxl=True оставляет их в виде .jxl."""
    dest = os.path.abspath(dest)
    os.makedirs(long_path(dest), exist_ok=True)
    djxl = None
    total = 0
    names = layout.list_names(out, name)
    sets = {nm: layout.shard_list(out, nm) for nm in names}
    size = sum(os.path.getsize(sh.path) for shards in sets.values() for sh in shards)
    with Progress("unpack", total=size) as bar:
        for nm in names:
            target = dest if flat else os.path.join(dest, nm)
            target_os = long_path(target)
            os.makedirs(target_os, exist_ok=True)
            shards = sets[nm]
            echo(f"Extracting {nm} to {target}: {len(shards)} shards")
            for shard in shards:
                bar.set_postfix(shard.filename)
                done_before = bar.n
                info = layout.read_shard_meta(out, nm, shard.index)
                jmap = {} if keep_jxl else jxl.member_map(info)
                expected = layout.read_manifest(shard.manifest) if jmap else {}
                if jmap and djxl is None:
                    djxl = jxl.require_tools(need_encoder=False)[1]
                n = 0
                with open_reader(shard.path, on_read=bar.update) as rd:
                    with tarfile.open(fileobj=rd, mode="r|") as tar:
                        for m in tar:
                            orig = jmap.get(m.name)
                            shown = orig or m.name
                            if patterns and not any(fnmatch.fnmatch(shown, p) for p in patterns):
                                continue
                            if orig is not None:
                                ok = _restore_jpeg(djxl, tar, m, orig, expected.get(orig), target_os)
                            else:
                                ok = extract_member(tar, m, target_os)
                            n += ok
                total += n
                bar.set(done_before + os.path.getsize(shard.path))
                echo(f"  {shard.filename}: {num(n)} files extracted")
    echo("Done.")
    return total
