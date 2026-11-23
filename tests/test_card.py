import json
import os

import pytest

from conftest import legacy_pack, make_tree, write_file
from shardkeep.card import CARD, SETTINGS, SNIPPET, write_card
from shardkeep.cli import main
from shardkeep.pack import pack


def run(args):
    with pytest.raises(SystemExit) as e:
        main(args)
    return e.value.code


def read(p):
    return open(p, encoding="utf-8").read()


@pytest.fixture
def two_sources(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_file(a, "x/1.json", b'{"a": 1}' * 100)
    write_file(b, "y/2.csv", b"1;2\n" * 100)
    return a, b


def test_pack_creates_card_once(tmp_path, two_sources):
    a, b = two_sources
    out = tmp_path / "out"
    assert run(["pack", str(a), str(out), "--level", "1", "--threads", "1", "--hf-repo", "user/ds",
                "--title", "My data"]) == 0
    card = read(out / CARD)
    assert "pretty_name: \"My data\"" in card and "# My data" in card
    assert "<!-- shardkeep:begin sets -->" in card and "| `a` |" in card
    assert "hf download user/ds" in card
    assert (out / "pack_shards.py").exists() and (out / SNIPPET).exists()
    settings = json.loads(read(out / SETTINGS))
    assert settings["hf_repo"] == "user/ds" and settings["title"] == "My data"

    edited = card.replace("# My data", "# My data, edited by hand") + "\nMy own notes.\n"
    (out / CARD).write_text(edited, encoding="utf-8")
    assert run(["pack", str(b), str(out), "--level", "1", "--threads", "1"]) == 0
    assert read(out / CARD) == edited  # pack не трогает существующую карточку


def test_card_updates_only_marked_regions(tmp_path, two_sources):
    a, b = two_sources
    out = tmp_path / "out"
    pack(a, out, level=1, threads=1)
    write_card(out, {"hf_repo": "user/ds"}, mode="create")
    edited = read(out / CARD).replace("## Contents", "## Contents\n\nHand-written description.") + "\nNotes.\n"
    (out / CARD).write_text(edited, encoding="utf-8")
    pack(b, out, level=1, threads=1)
    assert run(["card", str(out)]) == 0
    card = read(out / CARD)
    assert "Hand-written description." in card and card.endswith("Notes.\n")
    assert "| `a` |" in card and "| `b` |" in card
    assert "hf download user/ds" in card  # настройки запомнены


def test_card_with_crlf_line_endings(tmp_path, two_sources):
    """Карточка, сохраненная в Блокноте Windows (CRLF), обновляется и сохраняет переводы строк."""
    a, b = two_sources
    out = tmp_path / "out"
    pack(a, out, level=1, threads=1)
    write_card(out, {}, mode="create")
    crlf = read(out / CARD).replace("\n", "\r\n").encode("utf-8")
    (out / CARD).write_bytes(crlf)
    pack(b, out, level=1, threads=1)
    assert run(["card", str(out)]) == 0
    data = (out / CARD).read_bytes()
    assert b"| `b` |" in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_card_without_markers_is_left_alone(tmp_path, two_sources):
    a, _ = two_sources
    out = tmp_path / "out"
    pack(a, out, level=1, threads=1)
    (out / CARD).write_text("# Hand-made card\n", encoding="utf-8")
    assert run(["card", str(out)]) == 1
    assert read(out / CARD) == "# Hand-made card\n"
    assert run(["card", str(out), "--force"]) == 0
    assert "| `a` |" in read(out / CARD)
    assert read(out / (CARD + ".bak")) == "# Hand-made card\n"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "utf-8"])
def test_license_file_and_metadata(tmp_path, two_sources, encoding):
    a, _ = two_sources
    out = tmp_path / "out"
    pack(a, out, level=1, threads=1)
    lic = tmp_path / "license.txt"
    lic.write_text("Use only for research.\nSecond line.", encoding=encoding)
    assert run(["card", str(out), "--force", "--license", "other", "--license-name", "my-license",
                "--license-link", "https://example.org/terms", "--license-file", str(lic),
                "--tag", "robotics", "--tag", "trajectories", "--public"]) == 0
    card = read(out / CARD)
    assert "license: other\nlicense_name: my-license\nlicense_link: https://example.org/terms" in card
    assert "tags:\n- robotics\n- trajectories" in card
    assert "## License\n\nUse only for research.\nSecond line.\n" in card
    assert "﻿" not in card
    assert "hf auth login" not in card  # публичный датасет
    snippet = read(out / SNIPPET)
    assert "public Hugging Face dataset" in snippet and "https://example.org/terms" in snippet


def test_russian_card(tmp_path, two_sources):
    a, _ = two_sources
    out = tmp_path / "out"
    pack(a, out, level=1, threads=1)
    write_card(out, {"lang": "ru", "title": "Данные проекта"}, mode="update", force=True)
    card = read(out / CARD)
    assert "## Состав" in card and "## Скачивание и восстановление" in card
    assert "| Набор | Файлов |" in card
    assert "ё" not in card
    assert "приватном датасете" in read(out / SNIPPET)


def test_custom_template(tmp_path, two_sources, capsys):
    a, _ = two_sources
    out = tmp_path / "out"
    pack(a, out, level=1, threads=1)
    tpl = tmp_path / "tpl.md"
    tpl.write_text("# {{title}}\n<!-- shardkeep:begin sets -->\n{{sets}}\n<!-- shardkeep:end sets -->\n{{nope}}\n",
                   encoding="utf-8")
    capsys.readouterr()
    write_card(out, {"title": "T"}, mode="update", force=True, template=tpl)
    card = read(out / CARD)
    assert card.startswith("# T\n") and "| `a` |" in card and "{{nope}}" in card
    assert "unknown placeholders" in capsys.readouterr().out


def test_legacy_folder_card(tmp_path, legacy, capsys):
    """Карточка из исходного скрипта: без меток, с флагом waymo в card_settings.json."""
    src = tmp_path / "src"
    make_tree(src, long_paths=False)
    out = tmp_path / "out"
    legacy_pack(legacy, src, out, name="old")
    settings = json.loads(read(out / SETTINGS))
    settings["waymo"] = True
    (out / SETTINGS).write_text(json.dumps(settings), encoding="utf-8")
    before = read(out / CARD)
    capsys.readouterr()
    assert run(["card", str(out), "--hf-repo", "user/old"]) == 1
    text = capsys.readouterr().out
    assert "legacy \"waymo\" flag" in text and "no shardkeep markers" in text
    assert read(out / CARD) == before
    # скрипт восстановления заменен новым, команды в старой карточке продолжают работать
    assert "standalone restore script" in read(out / "pack_shards.py")
    assert os.path.exists(out / SNIPPET)
