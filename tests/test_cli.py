import subprocess
import sys

import pytest

from shardkeep import __version__
from shardkeep.cli import COMMANDS, main


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("cmd", sorted(COMMANDS))
def test_subcommand_help(cmd, capsys):
    with pytest.raises(SystemExit) as e:
        main([cmd, "--help"])
    assert e.value.code == 0
    assert capsys.readouterr().out


def test_module_entry_point():
    r = subprocess.run([sys.executable, "-m", "shardkeep", "--version"], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0
    assert "shardkeep" in r.stdout


def test_legacy_script_compiles(legacy):
    assert hasattr(legacy, "cmd_pack")
