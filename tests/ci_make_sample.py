"""Создает пример исходной папки и архив для проверки скрипта восстановления на старом Python в CI.

Запуск: python tests/ci_make_sample.py <папка>
Результат: <папка>/src (исходные файлы) и <папка>/out (архивы со скриптом pack_shards.py).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from conftest import make_tree  # noqa: E402
from shardkeep.card import copy_restore_script  # noqa: E402
from shardkeep.pack import pack  # noqa: E402

root = sys.argv[1]
make_tree(os.path.join(root, "src"), long_paths=False)
pack(os.path.join(root, "src"), os.path.join(root, "out"), name="s", level=3, threads=1, shard_gb=64 / 2**20)
copy_restore_script(os.path.join(root, "out"))
