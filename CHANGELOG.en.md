[Русский](CHANGELOG.md) | **English**

# Changelog

## 0.1.0 (in development)

First version of the package, based on the `pack_shards.py`, `analyze_workdir.py`, `compare_zip.py` and `check_hf_upload.py` scripts.

- One `shardkeep` command with the `analyze`, `pack`, `verify`, `unpack`, `upload`, `check-upload`, `check`, `card` and `compare-zip` subcommands.
- Archive format 2: parameters of every shard in `.meta.json`, set completeness, files that could not be read during packing. Archives of the original script are read and resumed; the original script reads the new archives.
- Compression profiles, level chosen by content, number of threads limited by free memory.
- `pack` no longer overwrites the dataset card. `card` updates only the parts between markers.
- License terms are no longer built into the code: the license is given as a file.
- Analyzer rules moved to TOML.
- Upload to Hugging Face and through rclone, with resuming and SHA-256 checks.
- Optional lossless conversion of JPEG to JPEG XL.
- Standalone restore script for Python 3.8+.
- Program messages in English, documentation in Russian and English.
- Progress line with an estimated time remaining for all long operations; `--no-progress` turns it off.
