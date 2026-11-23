import os
import zipfile

import pytest

from conftest import make_tree, write_file
from shardkeep.cli import main
from shardkeep.compare_zip import compare_zip
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path


def make_zip(src, zip_path, prefix="", skip=(), replace=None):
    replace = replace or {}
    root = long_path(str(src))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, names in os.walk(root):
            for n in names:
                p = os.path.join(dirpath, n)
                rel = os.path.relpath(p, root).replace("\\", "/")
                if rel in skip:
                    continue
                arc = prefix + rel
                if rel in replace:
                    z.writestr(arc, replace[rel])
                else:
                    z.write(p, arc)


@pytest.fixture
def folder(tmp_path):
    src = tmp_path / "folder"
    make_tree(src)
    return src


@pytest.mark.parametrize("prefix", ["", "folder/", "deep/top/"])
def test_matching_archive(tmp_path, folder, prefix):
    zp = tmp_path / "a.zip"
    make_zip(folder, zp, prefix=prefix)
    res = compare_zip(str(zp), str(folder), crc=True, test=True)
    assert res.complete and not res.only_in_zip and res.matched > 10


def test_differences_found(tmp_path, folder):
    data = bytearray(open(long_path(str(folder / "root_file.bin")), "rb").read())
    data[0] ^= 1  # тот же размер, другое содержимое
    zp = tmp_path / "a.zip"
    make_zip(folder, zp, skip={"tables/part_0.csv"},
             replace={"tables/part_1.csv": b"short", "root_file.bin": bytes(data)})
    with zipfile.ZipFile(zp, "a") as z:
        z.writestr("extra/only_in_zip.txt", b"x")
    res = compare_zip(str(zp), str(folder), crc=True)
    assert not res.complete
    assert res.only_in_folder == ["tables/part_0.csv"]
    assert res.size_mismatch == ["tables/part_1.csv"]
    assert res.crc_mismatch == ["root_file.bin"]
    assert res.only_in_zip == ["extra/only_in_zip.txt"]


def test_size_only_mode_misses_content_change_but_says_so(tmp_path, folder, capsys):
    zp = tmp_path / "a.zip"
    make_zip(folder, zp)
    res = compare_zip(str(zp), str(folder))
    assert res.complete
    assert "run with --crc" in capsys.readouterr().out


def test_damaged_archive(tmp_path, folder):
    zp = tmp_path / "a.zip"
    make_zip(folder, zp)
    data = bytearray(zp.read_bytes())
    data[200] ^= 0xFF  # внутри данных первого файла
    zp.write_bytes(bytes(data))
    res = compare_zip(str(zp), str(folder), test=True)
    assert res.damaged_member is not None and not res.complete


def test_cli(tmp_path, folder):
    zp = tmp_path / "a.zip"
    make_zip(folder, zp)
    with pytest.raises(SystemExit) as e:
        main(["compare-zip", str(zp), str(folder), "--crc", "--test"])
    assert e.value.code == 0
    (tmp_path / "x.zip").write_bytes(b"not a zip")
    with pytest.raises(SystemExit) as e:
        main(["compare-zip", str(tmp_path / "x.zip"), str(folder)])
    assert e.value.code == 2
    with pytest.raises(UserError):
        compare_zip(str(zp), str(tmp_path / "missing"))
