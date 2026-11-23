"""Распаковка архивов целиком или по шаблону."""

import fnmatch
import os
import tarfile

from shardkeep import layout
from shardkeep.codecs import open_reader
from shardkeep.fsutil import long_path
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


def unpack(out, dest, name=None, patterns=None, flat=False):
    dest = os.path.abspath(dest)
    os.makedirs(long_path(dest), exist_ok=True)
    total = 0
    for nm in layout.list_names(out, name):
        target = dest if flat else os.path.join(dest, nm)
        target_os = long_path(target)
        os.makedirs(target_os, exist_ok=True)
        shards = layout.shard_list(out, nm)
        print(f"Extracting {nm} to {target}: {len(shards)} shards")
        for shard in shards:
            n = 0
            with open_reader(shard.path) as rd:
                with tarfile.open(fileobj=rd, mode="r|") as tar:
                    for m in tar:
                        if patterns and not any(fnmatch.fnmatch(m.name, p) for p in patterns):
                            continue
                        if extract_member(tar, m, target_os):
                            n += 1
            total += n
            print(f"  {shard.filename}: {num(n)} files extracted")
    print("Done.")
    return total
