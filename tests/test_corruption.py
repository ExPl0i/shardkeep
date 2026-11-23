import json
import os

import pytest

from shardkeep import layout
from shardkeep.cli import main
from shardkeep.pack import pack
from shardkeep.verify import verify

SMALL = 64 / 2**20


@pytest.fixture(params=["zstd", "xz"])
def packed(request, tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", codec=request.param, level=3, threads=1, shard_gb=SMALL)
    assert verify(out).ok
    return out


def first_shard(out):
    return layout.shard_list(out, "s")[0]


def test_flipped_byte_detected(packed):
    p = first_shard(packed).path
    data = bytearray(open(p, "rb").read())
    data[len(data) // 2] ^= 0xFF
    open(p, "wb").write(bytes(data))
    res = verify(packed)
    assert not res.ok
    assert os.path.basename(p) in res.problems[0]


def test_truncated_shard_detected(packed):
    p = first_shard(packed).path
    data = open(p, "rb").read()
    open(p, "wb").write(data[: len(data) * 2 // 3])
    assert not verify(packed).ok


def test_manifest_hash_mismatch_detected(packed):
    mf = first_shard(packed).manifest
    lines = open(mf, encoding="utf-8").read().splitlines()
    r = json.loads(lines[0])
    r["sha256"] = "0" * 64
    lines[0] = json.dumps(r, ensure_ascii=False)
    open(mf, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    res = verify(packed)
    assert not res.ok
    assert "не совпадает" in res.problems[0]


def test_manifest_lists_missing_file(packed):
    mf = first_shard(packed).manifest
    with open(mf, "a", encoding="utf-8") as f:
        f.write(json.dumps({"path": "ghost.bin", "size": 1, "mtime": 0, "sha256": "0" * 64}) + "\n")
    res = verify(packed)
    assert not res.ok
    assert "нет 1 файлов" in res.problems[0]


def test_cli_exit_code_on_corruption(packed):
    p = first_shard(packed).path
    open(p, "wb").write(b"garbage")
    with pytest.raises(SystemExit) as e:
        main(["verify", str(packed)])
    assert e.value.code == 1
    report = open(os.path.join(packed, "verify_report.txt"), encoding="utf-8").read()
    assert report.split("\n")[0].endswith("ОШИБКИ")
