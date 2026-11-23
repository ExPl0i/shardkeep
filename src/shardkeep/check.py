"""shardkeep check: одна команда перед удалением исходных данных.

1. Архивы целы и совпадают с манифестами (полная проверка содержимого).
2. Исходные папки полностью упакованы и не менялись после упаковки (если они еще на диске).
3. Архивы загружены в хранилище и совпадают с локальными (если указано хранилище).
"""

import datetime as dt
import os

from shardkeep import layout
from shardkeep.errors import UserError
from shardkeep.fsutil import read_json, write_text
from shardkeep.verify import verify


def check(out, name=None, src=None, hf=None, rclone=None, allow_skipped=False, compute_hashes=True):
    names = layout.list_names(out, name)
    if src and len(names) > 1:
        raise UserError("With --src, choose the set with --name (or omit --src to use the source folder saved "
                        "with each set)")
    lines = []
    sources = {}
    ok = True

    print("Step 1. Archives and source folders\n")
    for nm in names:
        p = layout.settings_file(out, nm)
        saved = read_json(p).get("source") if os.path.exists(p) else None
        s = src or saved
        if s and not os.path.isdir(s):
            lines.append(f"set {nm}: source folder {s} is not on disk (already deleted?); only the archives are checked")
            s = None
        sources[nm] = s
        res = verify(out, name=nm, src=s, report=False, allow_skipped=allow_skipped)
        ok &= res.ok
        lines.append(f"set {nm}: archives {'OK' if res.ok else 'ERRORS'}, {res.shards} shards, {res.files} files"
                     + (f", source {s} checked" if s else ""))
        lines += [f"  {p}" for p in res.problems[:20]]
        print()

    remote = None
    if hf or rclone:
        print("Step 2. Upload\n")
        if hf:
            from shardkeep.remote import hf as backend
            remote = backend.check(out, hf, compute_hashes=compute_hashes)
        else:
            from shardkeep.remote import rclone as backend
            remote = backend.check(out, rclone, compute_hashes=compute_hashes)
        from shardkeep.remote import print_check
        print_check(remote)
        ok &= remote.ok
        lines.append(f"upload to {remote.target}: {'OK' if remote.ok else 'NOT COMPLETE'}, "
                     f"{remote.hash_checked} files checked by hash, {len(remote.size_only)} by size only")
        lines += [f"  missing or different: {p}" for p in remote.essential_problems[:20]]
    else:
        lines.append("upload: not checked (pass --hf or --rclone)")

    deletable = sorted({s for s in sources.values() if s})
    print("\n" + "=" * 70)
    for line in lines:
        print(line)
    if ok and remote is not None:
        verdict = "SAFE TO DELETE: everything is packed, verified and uploaded."
        if deletable:
            verdict += " Source folders: " + "; ".join(deletable)
    elif ok:
        verdict = "Archives are OK, but the upload was not checked. Do not delete the source data yet."
    else:
        verdict = "NOT SAFE TO DELETE: see the errors above."
    print("\n" + verdict)
    write_text(os.path.join(out, "check_report.txt"),
               f"{dt.datetime.now():%Y-%m-%d %H:%M} {'OK' if ok and remote is not None else 'NOT OK'}\n"
               + "\n".join(lines) + "\n\n" + verdict + "\n")
    return ok and remote is not None
