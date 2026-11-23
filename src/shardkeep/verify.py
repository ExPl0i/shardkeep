"""Проверка архивов по манифестам."""

import datetime as dt
import hashlib
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field

from shardkeep import jxl, layout
from shardkeep.codecs import drain, open_reader
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path, read_json, walk_files, write_text
from shardkeep.plan import load_plan
from shardkeep.progress import Progress, echo
from shardkeep.textfmt import num


@dataclass
class VerifyResult:
    ok: bool = True
    files: int = 0
    shards: int = 0
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def fail(self, msg):
        self.ok = False
        self.problems.append(msg)


def _check_jxl_member(fo, name, orig, info, expected, djxl, workdir, bad):
    """Файл, переведенный в JPEG XL: сверяется хеш .jxl, а если есть djxl, то и восстановленный JPEG."""
    rec = info["jxl"][orig]
    h = hashlib.sha256()
    tmpdir = tempfile.mkdtemp(prefix="sk_verify_", dir=workdir) if djxl else None
    try:
        out = open(os.path.join(tmpdir, "f.jxl"), "wb") if tmpdir else None
        try:
            for b in iter(lambda: fo.read(8 << 20), b""):
                h.update(b)
                if out:
                    out.write(b)
        finally:
            if out:
                out.close()
        if h.hexdigest() != rec["sha256"]:
            bad.append(f"content mismatch {name}")
            return
        exp = expected.get(orig)
        if djxl and exp:
            back = os.path.join(tmpdir, "f.jpg")
            if not jxl.decode(djxl, os.path.join(tmpdir, "f.jxl"), back):
                bad.append(f"djxl cannot decode {name}")
            elif os.path.getsize(back) != exp["size"] or jxl._sha256(back) != exp["sha256"]:
                bad.append(f"JPEG restored from {name} differs from the original")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def check_shard(shard, info=None, djxl=None, on_read=None):
    """Читает часть целиком и сверяет каждый файл с манифестом.

    info – параметры части (формат 2): дополнительно сверяются размер и SHA-256 самого файла части.
    djxl – команда djxl: файлы, переведенные в JPEG XL, восстанавливаются и сверяются с исходными.
    Возвращает (список проблем, число файлов, записи манифеста).
    """
    expected = layout.read_manifest(shard.manifest)
    jmap = jxl.member_map(info)
    seen = set()
    bad = []
    file_hash = hashlib.sha256()
    try:
        with open_reader(shard.path, hasher=file_hash, on_read=on_read) as rd:
            with tarfile.open(fileobj=rd, mode="r|") as tar:
                for m in tar:
                    if not m.isfile():
                        continue
                    fo = tar.extractfile(m)
                    if m.name in jmap:
                        orig = jmap[m.name]
                        _check_jxl_member(fo, m.name, orig, info, expected, djxl, os.path.dirname(shard.path), bad)
                        seen.add(orig)
                        continue
                    h = hashlib.sha256()
                    while True:
                        b = fo.read(8 << 20)
                        if not b:
                            break
                        h.update(b)
                    exp = expected.get(m.name)
                    if exp is None:
                        bad.append(f"unexpected file {m.name}")
                    elif exp["sha256"] != h.hexdigest() or exp["size"] != m.size:
                        bad.append(f"content mismatch {m.name}")
                    seen.add(m.name)
            drain(rd)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"archive is damaged: {exc}")
    else:
        if info and info.get("sha256"):
            if os.path.getsize(shard.path) != info.get("compressed_bytes") or file_hash.hexdigest() != info["sha256"]:
                bad.append("shard file differs from the one written during packing (size or SHA-256)")
    missing = set(expected) - seen
    if missing:
        bad.append(f"{len(missing)} files from the manifest are missing in the archive")
    return bad, len(seen), expected


def check_source(src, expected):
    """Число файлов из манифеста, которые в исходной папке изменились или исчезли."""
    src_os = long_path(src)
    changed = 0
    for p, r in expected.items():
        try:
            st = os.stat(os_path(src_os, p))
            if st.st_size != r["size"] or int(st.st_mtime) != r["mtime"]:
                changed += 1
        except OSError:
            changed += 1
    return changed


def _load_settings(out, name):
    p = layout.settings_file(out, name)
    return read_json(p) if os.path.exists(p) else {}


def _planned(out, name):
    p = layout.plan_file(out, name)
    if not os.path.exists(p):
        return None
    plan = load_plan(p)
    return (max(e["s"] for e in plan) + 1) if plan else 0


def _sets_for_src(out, names, src):
    """Наборы, которые сверяются с исходной папкой src."""
    if len(names) == 1:
        return set(names)
    src_abs = os.path.normcase(os.path.abspath(src))
    match = {n for n in names
             if os.path.normcase(os.path.abspath(_load_settings(out, n).get("source", ""))) == src_abs}
    if not match:
        raise UserError("The folder holds several sets and none was packed from the given source folder. "
                        "Choose a set with --name.")
    return match


def verify(out, name=None, src=None, report=True, allow_skipped=False):
    res = VerifyResult()
    djxl = jxl.find_tools()[1]
    names = layout.list_names(out, name)
    src_sets = _sets_for_src(out, names, src) if src else set()
    states = {nm: layout.scan_set(out, nm, _planned(out, nm)) for nm in names}
    total = sum(os.path.getsize(sh.path) for st in states.values() for sh in st.shards)
    with Progress("verify", total=total) as bar:
        for nm in names:
            _verify_set(res, out, nm, states[nm], src if nm in src_sets else None, allow_skipped, djxl, bar)
    _report(res, out, report)
    return res


def _verify_set(res, out, nm, state, src, allow_skipped, djxl, bar):
    jxl_warned = False
    settings = _load_settings(out, nm)
    echo(f"Verifying set {nm}: {len(state.shards)}"
         + (f" of {state.planned}" if state.planned is not None else "") + " shards")
    _check_completeness(res, state)
    n_files = 0
    archived = set()
    skipped_paths = set()
    for shard in state.shards:
        bar.set_postfix(shard.filename)
        done_before = bar.n
        info = layout.read_shard_meta(out, nm, shard.index)
        if info and info.get("jxl") and djxl is None and not jxl_warned:
            jxl_warned = True
            msg = ("JPEG XL files were checked by the hash of the .jxl data only: djxl is not installed, so the "
                   "restored JPEG files were not compared with the originals")
            echo("  Warning: " + msg)
            res.warnings.append(msg)
        bad, n, expected = check_shard(shard, info, djxl, on_read=bar.update)
        bar.set(done_before + os.path.getsize(shard.path))
        skipped = (info or {}).get("skipped", [])
        n_files += n
        archived.update(expected)
        skipped_paths.update(x["path"] for x in skipped)
        echo(f"  {shard.filename}: {'OK' if not bad else 'ERROR'}, {num(n)} files"
              + (f", unreadable during packing: {len(skipped)}" if skipped else ""))
        for b in bad[:10]:
            echo(f"    {b}")
        if bad:
            res.fail(f"{shard.path}: " + "; ".join(bad[:20]))
        if skipped:
            msg = (f"{shard.filename}: {len(skipped)} files could not be read during packing and are not in the archive: "
                   + ", ".join(s["path"] for s in skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
            echo("    " + msg)
            if allow_skipped:
                res.warnings.append(msg)
            else:
                res.fail(msg)
        if src:
            changed = check_source(src, expected)
            if changed:
                msg = f"{shard.filename}: {changed} source files changed or disappeared after packing"
                echo("    " + msg)
                res.fail(msg)
    if src:
        _check_new_files(res, src, settings, archived | skipped_paths, state)
    res.files += n_files
    res.shards += len(state.shards)
    echo(f"  files verified: {num(n_files)}")


def _report(res, out, report):
    if report:
        lines = [f"{dt.datetime.now():%d.%m.%Y %H:%M} {'OK' if res.ok else 'ERRORS'}"] + res.problems
        if res.warnings:
            lines += ["", "Warnings:"] + res.warnings
        write_text(os.path.join(out, "verify_report.txt"), "\n".join(lines) + "\n")
    if res.ok:
        echo("\nAll archives are intact and match their manifests. The source data can be deleted "
              "once the archives are uploaded to storage.")
    else:
        echo("\nErrors found (see verify_report.txt). Do not delete the source data.")


def _check_completeness(res, state):
    if state.missing_indexes:
        msg = (f"set {state.name}: packing is not finished, {len(state.shards)} of {state.planned} shards done; "
               "run pack again with the same arguments")
        echo("  " + msg)
        res.fail(msg)
    for fn in state.no_data:
        msg = f"set {state.name}: manifest {fn} exists but the shard file is missing"
        echo("  " + msg)
        res.fail(msg)
    for fn in state.no_manifest:
        msg = f"set {state.name}: shard {fn} is incomplete (no manifest)"
        echo("  " + msg)
        res.fail(msg)
    if state.partial:
        res.warnings.append(f"set {state.name}: temporary files of unfinished packing: " + ", ".join(state.partial))


def _check_new_files(res, src, settings, known, state):
    """Ищет в исходной папке файлы, которых нет ни в одном манифесте набора."""
    if state.planned is None or state.missing_indexes:
        return  # набор не полный: об этом уже сообщено
    with Progress(f"scanning {src}", unit="files") as bar:
        files = walk_files(src, settings.get("excludes", []), settings.get("include_env", False),
                           on_error=lambda msg: None, on_file=lambda size: bar.update())
    new = sorted(rel for rel, _, _ in files if rel not in known)
    if new:
        msg = (f"set {state.name}: {num(len(new))} files in the source folder are not in the archives: "
               + ", ".join(new[:5]) + (" ..." if len(new) > 5 else ""))
        echo("  " + msg)
        res.fail(msg)
