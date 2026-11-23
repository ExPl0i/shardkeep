import importlib.util
import os

import pytest

LEGACY_PATH = os.path.join(os.path.dirname(__file__), "legacy", "pack_shards_v0.py")


@pytest.fixture(scope="session")
def legacy():
    """Исходная версия pack_shards.py: создает архивы старого формата."""
    spec = importlib.util.spec_from_file_location("pack_shards_v0", LEGACY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
