"""rclone: загрузка в любое хранилище, настроенное в rclone (Яндекс Диск, Google Drive, S3 и другие).

Нужна программа rclone в PATH и настроенное хранилище (rclone config). Адрес задается как
имя_хранилища:путь, например yadisk:backup/dataset.
"""

import json
import shutil
import subprocess

from shardkeep.errors import UserError
from shardkeep.remote import IGNORE, RemoteFile, compare, describe, local_files


def _rclone():
    exe = shutil.which("rclone")
    if not exe:
        raise UserError("rclone is not installed or not in PATH: https://rclone.org/install/")
    return exe


def _run(args, capture=True):
    """Запускает rclone. Вынесено в отдельную функцию, чтобы тесты могли его подменить."""
    r = subprocess.run([_rclone(), *args], capture_output=capture, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()[-1:] if capture else []
        raise UserError(f"rclone {args[0]} failed (exit code {r.returncode})" + (f": {msg[0]}" if msg else ""))
    return r.stdout if capture else ""


def _check_target(target):
    if ":" not in target:
        raise UserError(f"rclone target must look like remote:path, for example yadisk:backup/dataset (got {target})")


def list_remote(target):
    _check_target(target)
    try:
        out = _run(["lsjson", "-R", "--files-only", "--hash", target])
    except UserError as exc:
        if "directory not found" in str(exc).lower():
            return {}
        raise
    remote = {}
    for item in json.loads(out or "[]"):
        hashes = {k.lower(): v for k, v in (item.get("Hashes") or {}).items()}
        remote[item["Path"]] = RemoteFile(item["Path"], int(item["Size"]), sha256=hashes.get("sha256"),
                                          other_hashes=hashes)
    return remote


def check(out, target, compute_hashes=True):
    return compare(f"rclone {target}", local_files(out), list_remote(target), compute_hashes)


def upload(out, target):
    _check_target(target)
    local = local_files(out)
    print(f"Uploading {describe(local)} to {target} with rclone (files that are already there are skipped)")
    args = ["copy", out, target, "--progress"]
    for p in IGNORE:
        args += ["--exclude", p]
    _run(args, capture=False)
    print("Done.")
    return len(local)
