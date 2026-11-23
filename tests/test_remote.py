"""Загрузка и сверка с хранилищем на поддельных Hugging Face и rclone (без сети)."""

import fnmatch
import hashlib
import json
import os
import shutil
import types

import pytest

from conftest import legacy_pack, make_tree, write_file
from shardkeep import remote
from shardkeep.cli import main
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path
from shardkeep.remote import hf, rclone

SMALL = 64 / 2**20
LFS_EXT = (".zst", ".xz", ".gz")


def run(args):
    with pytest.raises(SystemExit) as e:
        main(args)
    return e.value.code


# ---------------------------------------------------------------- поддельный Hugging Face
class NotFound(Exception):
    pass


class FakeHfApi:
    def __init__(self):
        self.repos = {}
        self.commits = []
        self.fail_after = None

    def repo_info(self, repo, repo_type=None):
        if repo not in self.repos:
            raise NotFound(repo)
        return types.SimpleNamespace(private=self.repos[repo]["private"])

    def create_repo(self, repo, repo_type=None, private=None, exist_ok=False):
        self.repos.setdefault(repo, {"private": private, "files": {}})

    def list_repo_tree(self, repo, repo_type=None, recursive=False):
        if repo not in self.repos:
            raise NotFound(repo)
        for path, data in sorted(self.repos[repo]["files"].items()):
            if path.endswith(LFS_EXT):
                lfs = types.SimpleNamespace(sha256=hashlib.sha256(data).hexdigest())
                yield types.SimpleNamespace(path=path, size=len(data), blob_id="x", lfs=lfs)
            else:
                oid = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
                yield types.SimpleNamespace(path=path, size=len(data), blob_id=oid, lfs=None)

    def upload_folder(self, repo_id, repo_type, folder_path, allow_patterns, commit_message):
        if self.fail_after is not None and len(self.commits) >= self.fail_after:
            raise ConnectionError("network is down")
        files = self.repos[repo_id]["files"]
        for dirpath, _, names in os.walk(folder_path):
            for n in names:
                p = os.path.join(dirpath, n)
                rel = os.path.relpath(p, folder_path).replace("\\", "/")
                if any(fnmatch.fnmatch(rel, pat) for pat in allow_patterns):
                    with open(p, "rb") as f:
                        files[rel] = f.read()
        self.commits.append(list(allow_patterns))


@pytest.fixture
def fake_hf(monkeypatch):
    api = FakeHfApi()
    monkeypatch.setattr(hf, "_api", lambda: api)
    monkeypatch.setattr(hf, "_not_found_errors", lambda: (NotFound,))
    return api


@pytest.fixture
def packed(tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    assert run(["pack", str(src), str(out), "--name", "s", "--level", "3", "--threads", "1",
                "--shard-gb", str(SMALL)]) == 0
    assert run(["verify", str(out), "--src", str(src)]) == 0
    (out / "data" / "s" / "leftover.partial").write_bytes(b"junk")
    return src, out


def test_upload_and_check(packed, fake_hf, capsys):
    src, out = packed
    assert run(["upload", str(out), "--hf", "user/ds"]) == 0
    repo = fake_hf.repos["user/ds"]
    assert repo["private"] is True
    paths = set(repo["files"])
    assert "README.md" in paths and "pack_shards.py" in paths and "manifest/s/plan.jsonl.gz" in paths
    assert not any(p.endswith(".partial") or p == "verify_report.txt" for p in paths)
    capsys.readouterr()
    assert run(["check-upload", str(out), "--hf", "user/ds"]) == 0
    text = capsys.readouterr().out
    assert "by size only: 0" in text and "all archives and manifests are uploaded" in text
    # повторная загрузка ничего не отправляет
    n_commits = len(fake_hf.commits)
    assert run(["upload", str(out), "--hf", "user/ds"]) == 0
    assert len(fake_hf.commits) == n_commits


def test_upload_resumes(packed, fake_hf, monkeypatch):
    _, out = packed
    monkeypatch.setattr(hf, "batches", lambda files: remote.batches(files, max_files=3))
    fake_hf.fail_after = 2
    with pytest.raises(ConnectionError):
        hf.upload(str(out), "user/ds")
    uploaded = set(fake_hf.repos["user/ds"]["files"])
    assert len(uploaded) == 6
    fake_hf.fail_after = None
    n = hf.upload(str(out), "user/ds")
    assert n == len(remote.local_files(str(out))) - 6  # загружается только недостающее
    assert hf.check(str(out), "user/ds").ok


def test_check_detects_bad_upload(packed, fake_hf, capsys):
    _, out = packed
    hf.upload(str(out), "user/ds")
    files = fake_hf.repos["user/ds"]["files"]
    shard = sorted(p for p in files if p.startswith("data/"))[0]
    data = bytearray(files[shard])
    data[10] ^= 1  # тот же размер, другое содержимое
    files[shard] = bytes(data)
    manifest = sorted(p for p in files if p.endswith(".jsonl"))[0]
    files[manifest] = files[manifest].replace(b"0", b"1", 1)
    del files[sorted(p for p in files if p.startswith("data/"))[-1]]
    files["data/s/stray.bin"] = b"x"
    res = hf.check(str(out), "user/ds")
    assert not res.ok
    assert shard in res.hash_mismatch and manifest in res.hash_mismatch
    assert len(res.missing) == 1 and res.extra == ["data/s/stray.bin"]
    assert run(["check-upload", str(out), "--hf", "user/ds"]) == 1
    # загрузка исправляет расхождения
    hf.upload(str(out), "user/ds")
    assert hf.check(str(out), "user/ds").ok


def test_service_file_difference_is_not_fatal(packed, fake_hf):
    _, out = packed
    hf.upload(str(out), "user/ds")
    (out / "README.md").write_text("# edited after upload\n", encoding="utf-8")
    res = hf.check(str(out), "user/ds")
    assert res.ok and res.size_mismatch + res.hash_mismatch == ["README.md"]


def test_size_only(packed, fake_hf):
    _, out = packed
    hf.upload(str(out), "user/ds")
    res = hf.check(str(out), "user/ds", compute_hashes=False)
    assert res.ok
    # SHA-256 частей известен из файлов параметров, остальное сверяется только по размеру
    assert res.hash_checked == len([p for p in fake_hf.repos["user/ds"]["files"] if p.startswith("data/")])
    assert res.size_only


def test_public_and_existing_visibility(packed, fake_hf, capsys):
    _, out = packed
    assert run(["upload", str(out), "--hf", "user/pub", "--public"]) == 0
    assert fake_hf.repos["user/pub"]["private"] is False
    capsys.readouterr()
    assert run(["upload", str(out), "--hf", "user/pub", "--private"]) == 0
    assert "already exists and is PUBLIC" in capsys.readouterr().out
    assert fake_hf.repos["user/pub"]["private"] is False


def test_legacy_archives_checked_by_hash(tmp_path, legacy, fake_hf):
    src = tmp_path / "src"
    make_tree(src, long_paths=False)
    out = tmp_path / "out"
    legacy_pack(legacy, src, out, name="old")
    hf.upload(str(out), "user/old")
    res = hf.check(str(out), "user/old")
    assert res.ok and not res.size_only  # SHA-256 частей посчитан по файлам


def test_upload_refuses_incomplete_set(packed, fake_hf):
    _, out = packed
    shard = sorted((out / "manifest" / "s").glob("s-*.jsonl"))[-1]
    shard.unlink()
    with pytest.raises(UserError, match="Not uploading"):
        remote.check_ready(str(out))
    assert run(["upload", str(out), "--hf", "user/ds"]) == 2


def test_missing_repo(packed, fake_hf):
    _, out = packed
    assert run(["check-upload", str(out), "--hf", "user/none"]) == 2


# ---------------------------------------------------------------- поддельный rclone
@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    storage = tmp_path / "cloud"

    def local(target):
        name, _, path = target.partition(":")
        return storage / name / path

    def fake_run(args, capture=True):
        if args[0] == "copy":
            src, dst = args[1], local(args[2])
            excludes = [args[i + 1] for i, a in enumerate(args) if a == "--exclude"]
            for dirpath, _, names in os.walk(src):
                for n in names:
                    p = os.path.join(dirpath, n)
                    rel = os.path.relpath(p, src).replace("\\", "/")
                    if any(fnmatch.fnmatch(rel, e) or fnmatch.fnmatch(n, e) for e in excludes):
                        continue
                    os.makedirs(os.path.dirname(dst / rel), exist_ok=True)
                    shutil.copy2(p, dst / rel)
            return ""
        if args[0] == "lsjson":
            root = local(args[-1])
            if not root.exists():
                raise UserError("rclone lsjson failed (exit code 3): directory not found")
            items = []
            for dirpath, _, names in os.walk(root):
                for n in names:
                    p = os.path.join(dirpath, n)
                    rel = os.path.relpath(p, root).replace("\\", "/")
                    data = open(p, "rb").read()
                    hashes = {"md5": hashlib.md5(data).hexdigest()}
                    if rel.startswith("data/"):  # как будто хранилище отдает SHA-256 только для части файлов
                        hashes["sha256"] = hashlib.sha256(data).hexdigest()
                    items.append({"Path": rel, "Size": len(data), "Hashes": hashes})
            return json.dumps(items)
        raise AssertionError(args)

    monkeypatch.setattr(rclone, "_run", fake_run)
    return local


def test_rclone_upload_and_check(packed, fake_rclone, capsys):
    _, out = packed
    assert run(["upload", str(out), "--rclone", "yadisk:backup/ds"]) == 0
    assert (fake_rclone("yadisk:backup/ds") / "README.md").exists()
    assert not list(fake_rclone("yadisk:backup/ds").rglob("*.partial"))
    res = rclone.check(str(out), "yadisk:backup/ds")
    assert res.ok and res.hash_checked > 0 and res.size_only  # манифесты сверены только по размеру
    shard = sorted(fake_rclone("yadisk:backup/ds").glob("data/s/*.tar.zst"))[0]
    data = bytearray(shard.read_bytes())
    data[5] ^= 1
    shard.write_bytes(bytes(data))
    assert run(["check-upload", str(out), "--rclone", "yadisk:backup/ds"]) == 1


def test_rclone_target_format(packed, fake_rclone):
    _, out = packed
    assert run(["upload", str(out), "--rclone", "no-colon"]) == 2
    res = rclone.check(str(out), "yadisk:nothing/here")
    assert not res.ok and res.missing


# ---------------------------------------------------------------- общая проверка
def test_check_command(packed, fake_hf, capsys):
    src, out = packed
    capsys.readouterr()
    assert run(["check", str(out)]) == 1
    assert "upload was not checked" in capsys.readouterr().out
    hf.upload(str(out), "user/ds")
    assert run(["check", str(out), "--hf", "user/ds"]) == 0
    text = capsys.readouterr().out
    assert "SAFE TO DELETE" in text and str(src) in text
    report = (out / "check_report.txt").read_text(encoding="utf-8")
    assert report.split("\n")[0].endswith(" OK")

    write_file(src, "new_after_packing.txt", b"new")
    assert run(["check", str(out), "--hf", "user/ds"]) == 1
    assert "NOT SAFE TO DELETE" in capsys.readouterr().out
    shutil.rmtree(long_path(str(src)))  # исходные данные уже удалены: проверяются только архивы и загрузка
    assert run(["check", str(out), "--hf", "user/ds"]) == 0
    assert "is not on disk" in capsys.readouterr().out
