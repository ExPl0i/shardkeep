"""Обход папки, поиск дубликатов, проверка git-репозиториев и оценка сжатия для shardkeep analyze."""

import fnmatch
import hashlib
import heapq
import lzma
import os
import random
import shutil
import subprocess
import zipfile
from collections import defaultdict

from shardkeep import codecs
from shardkeep.analyze.rules import file_ext
from shardkeep.fsutil import long_path
from shardkeep.progress import Progress
from shardkeep.textfmt import human, num, ts_local

ROOT_FILES_KEY = "<files in the root>"


class Stats:
    __slots__ = ("bytes", "files", "dirs", "small_files", "mtime_min", "mtime_max",
                 "cat_bytes", "cat_files", "ext_bytes", "ext_files")

    def __init__(self):
        self.bytes = self.files = self.dirs = self.small_files = 0
        self.mtime_min = self.mtime_max = None
        self.cat_bytes = defaultdict(int)
        self.cat_files = defaultdict(int)
        self.ext_bytes = defaultdict(int)
        self.ext_files = defaultdict(int)

    def add_file(self, size, mtime, cat, ext, small):
        self.bytes += size
        self.files += 1
        if small:
            self.small_files += 1
        if self.mtime_min is None or mtime < self.mtime_min:
            self.mtime_min = mtime
        if self.mtime_max is None or mtime > self.mtime_max:
            self.mtime_max = mtime
        self.cat_bytes[cat] += size
        self.cat_files[cat] += 1
        e = ext or "(no extension)"
        self.ext_bytes[e] += size
        self.ext_files[e] += 1

    def to_dict(self):
        return {
            "bytes": self.bytes, "files": self.files, "dirs": self.dirs, "small_files": self.small_files,
            "mtime_min": ts_local(self.mtime_min), "mtime_max": ts_local(self.mtime_max),
            "categories": {k: {"bytes": v, "files": self.cat_files[k]}
                           for k, v in sorted(self.cat_bytes.items(), key=lambda x: -x[1])},
        }


class Scanner:
    def __init__(self, root, rules, depth=2, top_n=300, dup_min=1 << 20, small_limit=512 << 10, excludes=(),
                 sample_k=12):
        self.root = os.path.abspath(root)
        self.root_os = long_path(root)
        self.rules = rules
        self.depth = depth
        self.top_n = top_n
        self.dup_min = dup_min
        self.small_limit = small_limit
        self.excludes = list(excludes)
        self.sample_k = sample_k
        self.nodes = defaultdict(Stats)          # ключ: кортеж частей пути
        self.largest = []                        # куча (размер, путь, категория)
        self.dup_candidates = defaultdict(list)  # размер -> [путь]
        self.samples = defaultdict(list)         # (папка первого уровня, категория) -> [(путь, размер)]
        self.sample_seen = defaultdict(int)
        self.repos = {}
        self.errors = []
        self.error_count = 0
        self.skipped_links = 0
        self.total_files = 0
        self.total_bytes = 0
        self._bar = None
        self.rng = random.Random(12345)

    def _err(self, path, exc):
        self.error_count += 1
        if len(self.errors) < 200:
            self.errors.append(f"{path}: {exc}")

    def _excluded(self, rel, name):
        return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in self.excludes)

    def scan(self):
        # Общий объем заранее неизвестен: строка прогресса показывает счетчик, скорость и прошедшее время.
        with Progress("scanning", unit="files") as bar:
            self._bar = bar
            self._scan()
        self._bar = None

    def _scan(self):
        # элемент стека: (путь ОС, части пути, состояние правил, внутри .git, репозиторий)
        stack = [(self.root_os, (), self.rules.ROOT_STATE, False, None)]
        while stack:
            path, parts, state, in_git, repo = stack.pop()
            rel_dir = "/".join(parts)
            try:
                with os.scandir(path) as it:
                    entries = list(it)
            except OSError as exc:
                self._err(rel_dir or ".", exc)
                continue
            if not in_git and any(e.name == ".git" for e in entries):
                repo = rel_dir or "."
                self.repos.setdefault(repo, {"path": repo, "worktree_bytes": 0, "git_bytes": 0,
                                             "worktree_files": 0})
            for e in entries:
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                if self.excludes and self._excluded(rel, e.name):
                    continue
                try:
                    is_link = e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction())
                except OSError:
                    is_link = False
                if is_link:
                    self.skipped_links += 1
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        child = parts + (e.name,)
                        for k in range(1, min(self.depth, len(child) - 1) + 1):
                            self.nodes[child[:k]].dirs += 1
                        self.nodes[()].dirs += 1
                        if self.rules.notices:
                            self.rules.check_notices(rel)
                        stack.append((e.path, child, self.rules.dir_state(state, e.name),
                                      in_git or e.name == ".git", repo))
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError as exc:
                    self._err(rel, exc)
                    continue
                self._add_file(e.name, rel, parts, st.st_size, st.st_mtime, state, repo)
                self._bar.postfix = human(self.total_bytes)
                self._bar.update()

    def _add_file(self, name, rel, parts, size, mtime, state, repo):
        lname = name.lower()
        ext = file_ext(lname)
        cat = self.rules.classify(lname, rel, size, state)
        if self.rules.notices:
            self.rules.check_notices(rel)
        small = size < self.small_limit
        self.total_files += 1
        self.total_bytes += size
        self.nodes[()].add_file(size, mtime, cat, ext, small)
        if not parts:
            self.nodes[(ROOT_FILES_KEY,)].add_file(size, mtime, cat, ext, small)
        else:
            for k in range(1, min(self.depth, len(parts)) + 1):
                self.nodes[parts[:k]].add_file(size, mtime, cat, ext, small)
        unit = parts[0] if parts else ROOT_FILES_KEY
        if len(self.largest) < self.top_n:
            heapq.heappush(self.largest, (size, rel, cat))
        elif size > self.largest[0][0]:
            heapq.heapreplace(self.largest, (size, rel, cat))
        if size >= self.dup_min and cat not in ("git", "env"):
            self.dup_candidates[size].append(rel)
        if repo is not None:
            r = self.repos[repo]
            if cat == "git":
                r["git_bytes"] += size
            else:
                r["worktree_bytes"] += size
                r["worktree_files"] += 1
        # резервуарная выборка для оценки сжатия
        if self.sample_k > 0 and size > 0 and cat != "git":
            key = (unit, cat)
            self.sample_seen[key] += 1
            bucket = self.samples[key]
            if len(bucket) < self.sample_k:
                bucket.append((rel, size))
            else:
                j = self.rng.randrange(self.sample_seen[key])
                if j < self.sample_k:
                    bucket[j] = (rel, size)

    def os_path(self, rel):
        return os.path.join(self.root_os, *rel.split("/"))


# ----------------------------------------------------------------------------
# Дубликаты
# ----------------------------------------------------------------------------
def _partial_hash(path, size, block=1 << 20):
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(block))
        if size > 2 * block:
            f.seek(size // 2)
            h.update(f.read(block))
            f.seek(-block, os.SEEK_END)
            h.update(f.read(block))
    return h.hexdigest()


def _full_hash(path, block=8 << 20):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(block), b""):
            h.update(b)
    return h.hexdigest()


def find_duplicates(scanner, mode):
    if mode == "off":
        return []
    cands = [(s, p) for s, p in scanner.dup_candidates.items() if len(p) > 1]
    total = sum(s * len(p) for s, p in cands)
    print(f"Looking for duplicates: {num(sum(len(p) for _, p in cands))} candidate files, {human(total)}")
    groups = []
    bar = Progress("duplicates", total=total)
    for size, paths in sorted(cands, key=lambda x: -x[0]):
        by_partial = defaultdict(list)
        for rel in paths:
            try:
                by_partial[_partial_hash(scanner.os_path(rel), size)].append(rel)
            except OSError as exc:
                scanner._err(rel, exc)
        for ph, same in by_partial.items():
            if len(same) < 2:
                continue
            if mode == "quick":
                groups.append({"size": size, "hash": "partial:" + ph, "paths": sorted(same), "verified": False})
                continue
            by_full = defaultdict(list)
            for rel in same:
                try:
                    by_full[_full_hash(scanner.os_path(rel))].append(rel)
                except OSError as exc:
                    scanner._err(rel, exc)
            for fh, eq in by_full.items():
                if len(eq) > 1:
                    groups.append({"size": size, "hash": fh, "paths": sorted(eq), "verified": True})
        bar.update(size * len(paths))
    bar.close()
    groups.sort(key=lambda g: -g["size"] * (len(g["paths"]) - 1))
    return groups


# ----------------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------------
def run_git(repo_path, *args, timeout=120):
    # safe.directory=*: git отказывается работать в папках другого владельца («dubious ownership»)
    cmd = ["git", "-c", "safe.directory=*", "-c", "core.quotepath=off", "-C", repo_path, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if r.returncode != 0:
        return None, r.stderr.decode("utf-8", errors="replace").strip()
    return r.stdout.decode("utf-8", errors="replace"), None


def inspect_repos(scanner):
    have_git = shutil.which("git") is not None
    result = []
    bar = Progress("git repositories", total=len(scanner.repos), unit="repos")
    for rel, info in sorted(scanner.repos.items()):
        bar.update()
        repo_path = scanner.root if rel == "." else os.path.join(scanner.root, *rel.split("/"))
        rec = dict(info)
        rec.update({"remotes": {}, "branch": "", "modified": None, "untracked": None, "unpushed": None,
                    "last_commit": "", "tracked_bytes": None, "lfs": False, "error": ""})
        try:
            with open(os.path.join(repo_path, ".gitattributes"), encoding="utf-8", errors="ignore") as f:
                rec["lfs"] = "filter=lfs" in f.read()
        except OSError:
            pass
        if not have_git:
            rec["error"] = "git is not in PATH"
            result.append(rec)
            continue
        out, err = run_git(repo_path, "remote", "-v")
        if out is not None:
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 2 and p[0] not in rec["remotes"]:
                    rec["remotes"][p[0]] = p[1]
        else:
            rec["error"] = err or ""
        out, _ = run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        rec["branch"] = (out or "").strip()
        out, err = run_git(repo_path, "status", "--porcelain=v1")
        if out is not None:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            rec["untracked"] = sum(1 for ln in lines if ln.startswith("??"))
            rec["modified"] = len(lines) - rec["untracked"]
        elif not rec["error"]:
            rec["error"] = err or ""
        out, _ = run_git(repo_path, "rev-list", "--count", "--branches", "--not", "--remotes")
        if out is not None and out.strip().isdigit():
            rec["unpushed"] = int(out.strip())
        out, _ = run_git(repo_path, "log", "-1", "--format=%ci")
        rec["last_commit"] = (out or "").strip()
        out, _ = run_git(repo_path, "ls-files", "-z", timeout=300)
        if out is not None:
            tracked = 0
            for name in out.split("\0"):
                if name:
                    try:
                        tracked += os.stat(long_path(os.path.join(repo_path, name))).st_size
                    except OSError:
                        pass
            rec["tracked_bytes"] = tracked
        result.append(rec)
    bar.close()
    return result


# ----------------------------------------------------------------------------
# Оценка сжатия
# ----------------------------------------------------------------------------
def compressor_name():
    return "zstd level 19" if codecs.have_zstd() else "xz (LZMA) level 6"


def compress_len(data):
    if not data:
        return 0
    if codecs._zstd is not None:
        return len(codecs._zstd.ZstdCompressor(level=19).compress(data))
    if codecs._stdzstd is not None:
        return len(codecs._stdzstd.compress(data, level=19))
    return len(lzma.compress(data, preset=6))


def inspect_npz(path):
    """(байт без сжатия, байт со сжатием) внутри npz."""
    stored = deflated = 0
    try:
        with zipfile.ZipFile(path) as z:
            for zi in z.infolist():
                if zi.compress_type == zipfile.ZIP_STORED:
                    stored += zi.file_size
                else:
                    deflated += zi.file_size
    except (OSError, zipfile.BadZipFile):
        pass
    return stored, deflated


def estimate_compression(scanner, budget_bytes):
    groups = {k: v for k, v in scanner.samples.items() if v}
    if budget_bytes <= 0 or not groups:
        return {}, {}, {}
    n_files = sum(len(v) for v in groups.values())
    chunk = max(256 << 10, min(4 << 20, budget_bytes // max(1, n_files)))
    print(f"Estimating compression ({compressor_name()}): {n_files} sample files, up to {human(chunk)} each")
    per_group = {}
    per_cat_raw = defaultdict(int)
    per_cat_comp = defaultdict(int)
    npz = {"checked": 0, "stored_bytes": 0, "deflated_bytes": 0}
    bar = Progress("compression estimate", total=len(groups), unit="groups")
    for (unit, cat), items in sorted(groups.items()):
        buf = bytearray()
        for rel, size in items:
            p = scanner.os_path(rel)
            try:
                with open(p, "rb") as f:
                    if size > chunk:
                        f.seek((size - chunk) // 2)
                    buf += f.read(chunk)
            except OSError as exc:
                scanner._err(rel, exc)
                continue
            if rel.lower().endswith(".npz"):
                s, d = inspect_npz(p)
                npz["checked"] += 1
                npz["stored_bytes"] += s
                npz["deflated_bytes"] += d
        if not buf:
            bar.update()
            continue
        comp = compress_len(bytes(buf))
        per_group[(unit, cat)] = (len(buf), comp)
        per_cat_raw[cat] += len(buf)
        per_cat_comp[cat] += comp
        bar.update()
    bar.close()
    per_cat = {c: per_cat_comp[c] / per_cat_raw[c] for c in per_cat_raw if per_cat_raw[c]}
    per_group_ratio = {k: c / r for k, (r, c) in per_group.items() if r}
    return per_group_ratio, per_cat, npz


def ratio_for(unit, cat, per_group, per_cat, sample_counts):
    r = per_group.get((unit, cat))
    if r is not None and sample_counts.get((unit, cat), 0) >= 3:
        return r
    if cat in per_cat:
        return per_cat[cat]
    return r if r is not None else 1.0
