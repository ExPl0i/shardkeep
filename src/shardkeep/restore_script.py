#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pack_shards.py - standalone restore script for archives made by shardkeep
(https://pypi.org/project/shardkeep/) or by the original pack_shards.py.

It verifies and extracts the archives in this folder. It does not need shardkeep:
Python 3.8+ and, for .tar.zst shards, the zstandard package (pip install zstandard)
or Python 3.14+ (module compression.zstd). To pack new data, install shardkeep.

Commands:
  Verify all archives against their manifests:
    python pack_shards.py verify <archive folder>

  Verify and also check that a source folder is fully packed and unchanged:
    python pack_shards.py verify <archive folder> --src <source folder> --name <set>

  Extract all sets (or one with --name; files matching --pattern only):
    python pack_shards.py unpack <archive folder> <destination>

JPEG files that were converted to JPEG XL during packing (shardkeep pack --jxl) are restored
byte for byte with djxl from libjxl (https://github.com/libjxl/libjxl/releases); with --keep-jxl
they are extracted as .jxl files.

Archive layout: data/<set>/<set>-NNNNN.tar.zst, manifest/<set>/<set>-NNNNN.jsonl.
"""

import argparse
import datetime as dt
import fnmatch
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

try:
    from compression import zstd as _stdzstd  # Python 3.14+
except Exception:  # noqa: BLE001
    _stdzstd = None
try:
    import zstandard as _zstd
except Exception:  # noqa: BLE001
    _zstd = None

ENV_DIR_NAMES = {"__pycache__", ".ipynb_checkpoints", ".mypy_cache", ".pytest_cache", "venv", ".venv",
                 "node_modules", ".git"}
SHARD_RE = re.compile(r"^(?P<name>.+)-(?P<i>\d{5,})\.tar\.(?P<ext>zst|xz)$")
MANIFEST_RE = re.compile(r"^(?P<name>.+)-(?P<i>\d{5,})\.jsonl$")


def long_path(p):
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)
    return p


def os_path(root, rel):
    return os.path.join(root, *rel.split("/"))


class HashingFile:
    def __init__(self, f, h):
        self.f, self.h = f, h

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b

    def readinto(self, buf):
        b = self.read(len(buf))
        buf[: len(b)] = b
        return len(b)

    def readable(self):
        return True

    def seekable(self):
        return False

    def close(self):
        self.f.close()

    @property
    def closed(self):
        return self.f.closed


def open_reader(path, h=None):
    raw = HashingFile(open(path, "rb"), h or hashlib.sha256())
    if path.endswith(".zst"):
        if _zstd is not None:
            return _zstd.ZstdDecompressor(max_window_size=2**31).stream_reader(raw, read_across_frames=True,
                                                                              closefd=True), raw
        if _stdzstd is not None:
            P = _stdzstd.DecompressionParameter
            return _stdzstd.ZstdFile(raw, "rb", options={P.window_log_max: 31}), raw
        raw.close()
        sys.exit("Reading .zst needs the zstandard package: pip install zstandard")
    if path.endswith(".xz"):
        return lzma.open(raw, "rb"), raw
    return raw, raw


def read_json(path):
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return json.loads(data.decode("utf-8"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def djxl_decode(djxl, fileobj, workdir):
    """Writes a .jxl member to a temporary file and restores the JPEG. Returns (jpeg path, temp folder) or None."""
    tmpdir = tempfile.mkdtemp(prefix="sk_djxl_", dir=workdir)
    src, dst = os.path.join(tmpdir, "f.jxl"), os.path.join(tmpdir, "f.jpg")
    with open(src, "wb") as f:
        shutil.copyfileobj(fileobj, f, 8 << 20)
    ok = subprocess.run([djxl, src, dst], capture_output=True).returncode == 0 and os.path.exists(dst)
    if not ok:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    return dst, tmpdir


def jxl_map(meta):
    return {v["member"]: k for k, v in ((meta or {}).get("jxl") or {}).items()}


def read_meta(mf):
    p = mf[:-len(".jsonl")] + ".meta.json"
    return read_json(p) if os.path.exists(p) else None


def read_manifest(path):
    with open(path, encoding="utf-8") as f:
        return {r["path"]: r for r in (json.loads(line) for line in f if line.strip())}


def set_names(out, name):
    if name:
        return [name]
    root = os.path.join(out, "manifest")
    if not os.path.isdir(root):
        sys.exit(f"{out} does not contain archives (no manifest folder)")
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def shards_of(out, name):
    """[(index, shard file, manifest)] and the problems found in the set layout."""
    data, man = os.path.join(out, "data", name), os.path.join(out, "manifest", name)
    shards, problems = [], []
    data_files = sorted(os.listdir(data)) if os.path.isdir(data) else []
    for fn in data_files:
        m = SHARD_RE.match(fn)
        if not m:
            continue
        mf = os.path.join(man, fn.split(".tar.")[0] + ".jsonl")
        if os.path.exists(mf):
            shards.append((int(m.group("i")), os.path.join(data, fn), mf))
        else:
            problems.append(f"set {name}: shard {fn} is incomplete (no manifest)")
    have = {os.path.basename(s[2]) for s in shards}
    for fn in sorted(os.listdir(man)) if os.path.isdir(man) else []:
        if MANIFEST_RE.match(fn) and fn not in have:
            problems.append(f"set {name}: manifest {fn} exists but the shard file is missing")
    plan = os.path.join(man, "plan.jsonl.gz")
    if os.path.exists(plan):
        with gzip.open(plan, "rt", encoding="utf-8") as f:
            planned = 1 + max((json.loads(line)["s"] for line in f if line.strip()), default=-1)
        if len(shards) < planned:
            problems.append(f"set {name}: packing is not finished, {len(shards)} of {planned} shards done")
    return shards, problems


def check_shard(path, mf, meta, djxl=None):
    expected = read_manifest(mf)
    jmap = jxl_map(meta)
    seen, bad = set(), []
    h_file = hashlib.sha256()
    raw = None
    try:
        rd, raw = open_reader(path, h_file)
        with rd:
            with tarfile.open(fileobj=rd, mode="r|") as tar:
                for m in tar:
                    if not m.isfile():
                        continue
                    fo = tar.extractfile(m)
                    if m.name in jmap:
                        orig = jmap[m.name]
                        seen.add(orig)
                        if djxl:
                            got = djxl_decode(djxl, fo, os.path.dirname(path))
                            exp = expected.get(orig)
                            if got is None:
                                bad.append(f"djxl cannot decode {m.name}")
                            else:
                                if exp and sha256_file(got[0]) != exp["sha256"]:
                                    bad.append(f"JPEG restored from {m.name} differs from the original")
                                shutil.rmtree(got[1], ignore_errors=True)
                        else:
                            h = hashlib.sha256()
                            for b in iter(lambda: fo.read(8 << 20), b""):
                                h.update(b)
                            if h.hexdigest() != meta["jxl"][orig]["sha256"]:
                                bad.append(f"content mismatch {m.name}")
                        continue
                    h = hashlib.sha256()
                    for b in iter(lambda: fo.read(8 << 20), b""):
                        h.update(b)
                    exp = expected.get(m.name)
                    if exp is None:
                        bad.append(f"unexpected file {m.name}")
                    elif exp["sha256"] != h.hexdigest() or exp["size"] != m.size:
                        bad.append(f"content mismatch {m.name}")
                    seen.add(m.name)
            while rd.read(8 << 20):
                pass
            while raw.read(8 << 20):
                pass
    except Exception as exc:  # noqa: BLE001
        bad.append(f"archive is damaged: {exc}")
    else:
        raw.close()
        if meta and meta.get("sha256") and (os.path.getsize(path) != meta.get("compressed_bytes")
                                            or h_file.hexdigest() != meta["sha256"]):
            bad.append("shard file differs from the one written during packing (size or SHA-256)")
    finally:
        if raw is not None:
            raw.close()
    missing = set(expected) - seen
    if missing:
        bad.append(f"{len(missing)} files from the manifest are missing in the archive")
    return bad, len(seen), expected


def walk(src, excludes, include_env):
    root = long_path(src)
    out, stack = [], [(root, ())]
    while stack:
        path, parts = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for e in entries:
            rel = "/".join(parts + (e.name,))
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(e.name, p) for p in excludes):
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if include_env or e.name.lower() not in ENV_DIR_NAMES:
                        stack.append((e.path, parts + (e.name,)))
                elif e.is_file(follow_symlinks=False):
                    out.append(rel)
            except OSError:
                pass
    return out


def cmd_verify(a):
    problems, warnings = [], []
    names = set_names(a.out, a.name)
    if a.src and len(names) > 1:
        sys.exit("With --src, choose the set with --name")
    total = 0
    djxl = shutil.which("djxl")
    for name in names:
        man = os.path.join(a.out, "manifest", name)
        settings_path = os.path.join(man, "pack_settings.json")
        settings = read_json(settings_path) if os.path.exists(settings_path) else {}
        shards, layout_problems = shards_of(a.out, name)
        print(f"Verifying set {name}: {len(shards)} shards")
        for p in layout_problems:
            print("  " + p)
        problems += layout_problems
        known = set()
        for i, path, mf in shards:
            meta = read_meta(mf)
            if meta and meta.get("jxl") and not djxl and "djxl" not in " ".join(warnings):
                warnings.append("JPEG XL files were checked by the hash of the .jxl data only (djxl is not installed)")
                print("  Warning: " + warnings[-1])
            bad, n, expected = check_shard(path, mf, meta, djxl)
            skipped = (meta or {}).get("skipped", [])
            total += n
            known.update(expected)
            known.update(s["path"] for s in skipped)
            print(f"  {os.path.basename(path)}: {'OK' if not bad else 'ERROR'}, {n} files")
            for b in bad[:10]:
                print("    " + b)
            if bad:
                problems.append(f"{path}: " + "; ".join(bad[:20]))
            if skipped:
                msg = f"{os.path.basename(path)}: {len(skipped)} files could not be read during packing"
                print("    " + msg)
                (warnings if a.allow_skipped else problems).append(msg)
            if a.src:
                changed = 0
                for p, r in expected.items():
                    try:
                        st = os.stat(os_path(long_path(a.src), p))
                        changed += st.st_size != r["size"] or int(st.st_mtime) != r["mtime"]
                    except OSError:
                        changed += 1
                if changed:
                    msg = f"{os.path.basename(path)}: {changed} source files changed or disappeared after packing"
                    print("    " + msg)
                    problems.append(msg)
        if a.src and not layout_problems:
            new = [p for p in walk(a.src, settings.get("excludes", []), settings.get("include_env", False))
                   if p not in known]
            if new:
                msg = f"set {name}: {len(new)} files in the source folder are not in the archives: " + ", ".join(
                    sorted(new)[:5])
                print("  " + msg)
                problems.append(msg)
    report = [f"{dt.datetime.now():%d.%m.%Y %H:%M} {'OK' if not problems else 'ERRORS'}"] + problems
    if warnings:
        report += ["", "Warnings:"] + warnings
    try:
        with open(os.path.join(a.out, "verify_report.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(report) + "\n")
    except OSError:
        pass
    print(f"Files verified: {total}")
    if problems:
        print("\nErrors found (see verify_report.txt). Do not delete the source data.")
        sys.exit(1)
    print("\nAll archives are intact and match their manifests.")


def safe_name(name):
    return not (name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                or (len(name) > 1 and name[1] == ":"))


def cmd_unpack(a):
    dest = os.path.abspath(a.dest)
    for name in set_names(a.out, a.name):
        target = long_path(dest if a.flat else os.path.join(dest, name))
        os.makedirs(target, exist_ok=True)
        shards, _ = shards_of(a.out, name)
        print(f"Extracting {name}: {len(shards)} shards")
        for _, path, mf in shards:
            n = 0
            meta = read_meta(mf)
            jmap = {} if a.keep_jxl else jxl_map(meta)
            expected = read_manifest(mf) if jmap else {}
            djxl = shutil.which("djxl") if jmap else None
            if jmap and not djxl:
                sys.exit("This set contains JPEG files converted to JPEG XL. Install djxl from libjxl "
                         "(https://github.com/libjxl/libjxl/releases) to restore them, or use --keep-jxl.")
            rd, raw = open_reader(path)
            try:
                with rd:
                    with tarfile.open(fileobj=rd, mode="r|") as tar:
                        for m in tar:
                            orig = jmap.get(m.name)
                            if a.pattern and not any(fnmatch.fnmatch(orig or m.name, p) for p in a.pattern):
                                continue
                            if orig is not None:
                                if not safe_name(orig):
                                    continue
                                got = djxl_decode(djxl, tar.extractfile(m), target)
                                exp = expected.get(orig)
                                if got is None or (exp and sha256_file(got[0]) != exp["sha256"]):
                                    sys.exit(f"Could not restore {orig} from {m.name} byte for byte")
                                dst = os_path(target, orig)
                                os.makedirs(os.path.dirname(dst), exist_ok=True)
                                shutil.move(got[0], dst)
                                shutil.rmtree(got[1], ignore_errors=True)
                                os.utime(dst, (m.mtime, m.mtime))
                                n += 1
                                continue
                            try:
                                tar.extract(m, target, filter="data")
                            except TypeError:  # Python without the filter argument
                                if not safe_name(m.name):
                                    continue
                                tar.extract(m, target)
                            n += 1
            finally:
                raw.close()
            print(f"  {os.path.basename(path)}: {n} files extracted")
    print("Done.")


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    ap = argparse.ArgumentParser(description="Verify and extract shardkeep archives.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("verify", help="verify archives against their manifests")
    p.add_argument("out", help="archive folder")
    p.add_argument("--name", help="verify only this set")
    p.add_argument("--src", help="source folder: check that all its files are packed and unchanged")
    p.add_argument("--allow-skipped", action="store_true",
                   help="do not treat files that could not be read during packing as an error")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("unpack", help="extract archives")
    p.add_argument("out", help="archive folder")
    p.add_argument("dest", help="destination folder")
    p.add_argument("--name", help="extract only this set")
    p.add_argument("--pattern", action="append", help="extract only files matching a pattern (repeatable)")
    p.add_argument("--flat", action="store_true", help="do not create a subfolder named after the set")
    p.add_argument("--keep-jxl", action="store_true", help="extract JPEG XL files as .jxl (no djxl needed)")
    p.set_defaults(func=cmd_unpack)
    for cmd in ("pack", "card"):
        p = sub.add_parser(cmd, help="not available in this script, install shardkeep")
        p.add_argument("args", nargs="*")
        p.set_defaults(func=lambda a: sys.exit(f"This restore script cannot run '{a.cmd}'. "
                                               "Install shardkeep: pip install shardkeep"))
    a = ap.parse_args()
    if not getattr(a, "func", None):
        ap.print_help()
        sys.exit(2)
    a.func(a)


if __name__ == "__main__":
    main()
