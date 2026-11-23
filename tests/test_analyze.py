import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import write_file
from shardkeep.analyze import analyze
from shardkeep.analyze.rules import load_rules
from shardkeep.errors import UserError

CASES = {
    "proj/.git/objects/ab/cdef": "git",
    "proj/__pycache__/m.cpython-313.pyc": "env",
    "proj/venv/lib/site.py": "env",
    "proj/src/model.py": "code",
    "proj/requirements.txt": "code",
    "proj/config.json": "code",
    "datasets/yolo/labels/train/img_0001.txt": "tables",   # разметка YOLO – данные, а не код
    "raw/validation_tfexample.tfrecord-00000-of-00150": "arrays",
    "data/tensors/batch_01.pt": "arrays",
    "checkpoints/epoch_10.pt": "checkpoint",
    "models/scaler.pkl": "checkpoint",
    "data/features.pkl": "arrays",
    "runs/exp1/events.out.tfevents.1700000000.host": "logs",
    "wandb/run-1/files/output.log": "logs",
    "wandb/run-1/files/model.pt": "checkpoint",
    "images/a.jpg": "images",
    "videos/b.mp4": "video",
    "backup/c.tar.zst": "archives",
    "tables/d.csv": "tables",
    "blob.xyz": "other",
}


def classify(rules, rel, size=100):
    state = rules.ROOT_STATE
    parts = rel.split("/")
    for d in parts[:-1]:
        state = rules.dir_state(state, d)
    name = parts[-1].lower()
    return rules.classify(name, rel, size, state)


@pytest.mark.parametrize("rel,expected", sorted(CASES.items()))
def test_default_classification(rel, expected):
    assert classify(load_rules(), rel) == expected


def test_json_size_rule():
    rules = load_rules()
    assert classify(rules, "x/small.json", 500) == "code"
    assert classify(rules, "x/scene.json", 50_000) == "tables"


def test_no_project_specific_rules():
    text = open(os.path.join(os.path.dirname(__file__), "..", "src", "shardkeep", "analyze", "rules.toml"),
                encoding="utf-8").read()
    active = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "waymo" not in active.lower()
    assert "raw_waymo" not in text


def test_user_rules(tmp_path):
    f = tmp_path / "my_rules.toml"
    f.write_text('''
[[category]]
id = "annotations"
title = "Annotations"
data = true
advice = "Keep them."

[[category]]
id = "code"
title = "My code"

[[rule]]
category = "annotations"
ext = [".txt"]
dir = ["labels"]

[[notice]]
path = ["*secret_project*"]
text = "Do not publish this data."
''', encoding="utf-16")  # так пишет перенаправление > в PowerShell 5.1
    rules = load_rules(str(f))
    assert classify(rules, "ds/labels/1.txt") == "annotations"
    assert classify(rules, "ds/notes/1.txt") == "tables"  # встроенные правила продолжают работать
    assert rules.categories["code"].title == "My code"
    only = load_rules(str(f), use_defaults=False)
    assert classify(only, "src/a.py") == "other"


@pytest.mark.parametrize("body,msg", [
    ('[[rule]]\ncategory = "code"\nextension = [".x"]\n', "unknown keys"),
    ('[[rule]]\ncategory = "nope"\n', "unknown category"),
    ('[[rule]]\ncategory = "code"\nmax_size = "lots"\n', "cannot read size"),
    ('[[rule\n', "invalid TOML"),
])
def test_invalid_rules(tmp_path, body, msg):
    f = tmp_path / "bad.toml"
    f.write_text(body, encoding="utf-8")
    with pytest.raises(UserError, match=msg):
        load_rules(str(f))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "work"
    big = os.urandom(2 << 20)
    write_file(root, "data/run1/weights.bin", big)
    write_file(root, "backup/run1_copy/weights.bin", big)  # дубликат
    for i in range(30):
        write_file(root, f"data/tables/t{i}.csv", ("1,2,3\n" * 3000).encode())
    write_file(root, "data/images/a.png", os.urandom(300_000))
    write_file(root, "code/main.py", b"print(1)\n")
    write_file(root, "secret_project/x.txt", b"x")
    return root


def test_analyze_report(tmp_path, project):
    rules = tmp_path / "rules.toml"
    rules.write_text('[[notice]]\npath = ["*secret_project*"]\ntext = "Do not publish this data."\n',
                     encoding="utf-8-sig")
    git = shutil.which("git")
    if git:
        subprocess.run([git, "init", "-q", str(project / "code")], check=True)
    out = tmp_path / "report"
    analyze(str(project), out=str(out), min_report_mb=0, rules_file=str(rules), csv_delimiter=";")
    for name in ("report.md", "summary.json", "folders.csv", "extensions.csv", "largest_files.csv",
                 "duplicates.csv", "git_repos.csv"):
        assert (out / name).exists(), name
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Groups of identical files: 1" in report
    assert "## Notices" in report and "Do not publish this data." in report
    assert "shardkeep pack" in report
    assert not re.search(r"[А-Яа-яЁё]", report)  # сообщения программы на английском
    if git:
        assert "No remote repository" in report
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["duplicate_groups"] == 1
    assert summary["compression_ratio_by_category"]["tables"] < 0.1
    header = (out / "folders.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("folder;level;bytes")


def test_analyze_cli(tmp_path, project):
    from shardkeep.cli import main
    with pytest.raises(SystemExit) as e:
        main(["analyze", str(project), "--out", str(tmp_path / "r"), "--dup-mode", "off", "--sample-mb", "0",
              "--no-git"])
    assert e.value.code == 0
    report = (tmp_path / "r" / "report.md").read_text(encoding="utf-8")
    assert "Duplicate search is disabled." in report and "Git check is disabled." in report
