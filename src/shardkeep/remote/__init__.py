"""Загрузка архивов в хранилище и сверка загруженного с локальной папкой.

Хранилища: Hugging Face (датасет, приватный или публичный) и любое хранилище rclone (Яндекс Диск,
Google Drive, S3 и другие). Сверка идет по списку файлов, размерам и, где хранилище отдает хеш,
по SHA-256 без скачивания.
"""

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field

from shardkeep import layout
from shardkeep.errors import UserError
from shardkeep.fsutil import read_json
from shardkeep.textfmt import human, num

# Служебные файлы, которые не загружаются.
IGNORE = ["*.partial", "*.bak", "verify_report.txt", "check_report.txt", ".cache/*", "*/.cache/*"]
# Файлы, без которых восстановление невозможно.
ESSENTIAL_PREFIXES = ("data/", "manifest/")


@dataclass
class LocalFile:
    rel: str
    path: str
    size: int
    sha256: object = None   # известен из параметров части (формат 2) или посчитан


@dataclass
class RemoteFile:
    rel: str
    size: int
    sha256: object = None
    git_sha1: object = None
    other_hashes: dict = field(default_factory=dict)


@dataclass
class RemoteCheck:
    target: str
    local_files: int = 0
    remote_files: int = 0
    missing: list = field(default_factory=list)
    size_mismatch: list = field(default_factory=list)
    hash_mismatch: list = field(default_factory=list)
    hash_checked: int = 0
    size_only: list = field(default_factory=list)
    extra: list = field(default_factory=list)

    @property
    def essential_problems(self):
        return [p for p in self.missing + self.size_mismatch + self.hash_mismatch if p.startswith(ESSENTIAL_PREFIXES)]

    @property
    def ok(self):
        return not self.essential_problems

    def problem_paths(self):
        return set(self.missing + self.size_mismatch + self.hash_mismatch)


def ignored(rel):
    return any(fnmatch.fnmatch(rel, p) for p in IGNORE)


def local_files(out):
    """Файлы папки с архивами, которые загружаются в хранилище."""
    if not os.path.isdir(os.path.join(out, "manifest")):
        raise UserError(f"{out} does not contain shardkeep archives (no manifest folder)")
    known = _known_shard_hashes(out)
    result = []
    for dirpath, dirs, names in os.walk(out):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, out).replace("\\", "/")
            if ignored(rel):
                continue
            result.append(LocalFile(rel, p, os.path.getsize(p), known.get(rel)))
    return sorted(result, key=lambda f: f.rel)


def _known_shard_hashes(out):
    """SHA-256 файлов частей из файлов параметров (формат 2): {путь: sha256}."""
    known = {}
    for name in layout.list_names(out):
        for sh in layout.shard_list(out, name):
            info = layout.read_shard_meta(out, name, sh.index)
            if info and info.get("sha256") and info.get("compressed_bytes") == os.path.getsize(sh.path):
                known[f"data/{name}/{sh.filename}"] = info["sha256"]
    return known


def sha256_file(path, block=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(block), b""):
            h.update(b)
    return h.hexdigest()


def git_blob_sha1(path):
    """Идентификатор файла в git (так Hugging Face хранит небольшие файлы вне LFS)."""
    with open(path, "rb") as f:
        data = f.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def compare(target, local, remote, compute_hashes=True):
    """Сверяет локальные файлы со списком файлов хранилища."""
    res = RemoteCheck(target, len(local), len(remote))
    local_set = set()
    for f in local:
        local_set.add(f.rel)
        r = remote.get(f.rel)
        if r is None:
            res.missing.append(f.rel)
            continue
        if r.size != f.size:
            res.size_mismatch.append(f.rel)
            continue
        if r.sha256:
            sha = f.sha256 or (sha256_file(f.path) if compute_hashes else None)
            if sha is None:
                res.size_only.append(f.rel)
            elif sha != r.sha256:
                res.hash_mismatch.append(f.rel)
            else:
                res.hash_checked += 1
        elif r.git_sha1 and compute_hashes:
            if git_blob_sha1(f.path) != r.git_sha1:
                res.hash_mismatch.append(f.rel)
            else:
                res.hash_checked += 1
        else:
            res.size_only.append(f.rel)
    res.extra = sorted(p for p in remote if p not in local_set and not ignored(p))
    return res


def print_check(res):
    print(f"Local: {num(res.local_files)} files. In {res.target}: {num(res.remote_files)} files.")
    print(f"Content checked by hash: {num(res.hash_checked)} files; by size only: {num(len(res.size_only))}.")
    for title, items in (("Not uploaded", res.missing), ("Size differs", res.size_mismatch),
                         ("Content differs (hash)", res.hash_mismatch)):
        if items:
            print(f"\n{title} ({num(len(items))}):")
            for p in items[:30]:
                print("  " + p)
    if res.extra:
        print(f"\nOnly in {res.target}, not in the local folder ({num(len(res.extra))}): "
              + ", ".join(res.extra[:10]) + (" ..." if len(res.extra) > 10 else ""))
    if res.ok:
        print("\nResult: all archives and manifests are uploaded and match the local files.")
        if res.missing or res.size_mismatch or res.hash_mismatch:
            print("Differences are only in service files outside data/ and manifest/; they do not affect restoring.")
    else:
        print("\nResult: the upload is NOT complete. Do not delete the source data.")


def check_ready(out, force=False):
    """Перед загрузкой: все наборы упакованы полностью, последняя проверка прошла успешно."""
    problems = []
    newest = 0.0
    for name in layout.list_names(out):
        p = layout.summary_file(out, name)
        s = read_json(p) if os.path.exists(p) else {}
        if s.get("complete") is False:
            problems.append(f"set {name}: packing is not finished ({s.get('shards')} of {s.get('planned_shards')} "
                            "shards)")
        st = layout.scan_set(out, name)
        problems += [f"set {name}: shard {fn} is incomplete (no manifest)" for fn in st.no_manifest]
        for sh in st.shards:
            newest = max(newest, os.path.getmtime(sh.manifest))
    if problems and not force:
        raise UserError("Not uploading: " + "; ".join(problems) + ". Finish packing first, or use --force.")
    report = os.path.join(out, "verify_report.txt")
    status = None
    if os.path.exists(report):
        with open(report, encoding="utf-8") as f:
            first = f.readline().strip()
        status = first.endswith(" OK") and os.path.getmtime(report) >= newest
    if not status:
        print("Warning: the archives have not been verified since the last packing (or verification found errors). "
              "Run shardkeep verify before deleting anything.")


def batches(files, max_files=50, max_bytes=20 * 2**30):
    """Делит файлы на порции для загрузки: прерванная загрузка продолжается с первой незагруженной порции."""
    batch, size = [], 0
    for f in files:
        if batch and (len(batch) >= max_files or size + f.size > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(f)
        size += f.size
    if batch:
        yield batch


def describe(files):
    return f"{num(len(files))} files, {human(sum(f.size for f in files))}"
