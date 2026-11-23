[Русский](format.md) | **English**

# shardkeep archive format

This document describes the layout of an archive folder. With it, the data can be verified and extracted without shardkeep, using the standard `zstd`, `xz` and `tar` programs.

## Layout

```
OUT/
  README.md                                dataset card (optional)
  pack_shards.py                           restore script (optional)
  verify_report.txt                        result of the last verification
  data/<set>/<set>-00000.tar.zst           shards of the set
  data/<set>/<set>-00001.tar.zst
  manifest/<set>/<set>-00000.jsonl         shard manifest
  manifest/<set>/<set>-00000.meta.json     shard parameters (format 2)
  manifest/<set>/plan.jsonl.gz             packing plan
  manifest/<set>/pack_settings.json        set parameters
  manifest/<set>/summary.json              set summary
```

A **set** is the content of one source folder. One archive folder may hold several sets.

A **shard** is a tar archive compressed with zstd (`.tar.zst`) or xz (`.tar.xz`). Each shard holds about `shard_gb` gigabytes of source data (4 by default). Shard numbers have five digits and start at `00000`.

## Shard

- tar format: POSIX pax. Member paths are relative, use `/` as the separator and have no leading `./`.
- Only regular files are stored. Symbolic links are skipped, empty folders are not kept.
- Each file keeps its modification time (whole seconds). The mode is always `0644`, no owner is recorded.
- zstd: every frame carries a checksum. In long-distance matching mode the window is 128 MB (`window_log` 27), so any zstd program can decompress the shards without `--long`. Multithreaded packing may write several zstd frames into one file; standard tools read them as one stream.
- The plan sorts files by top-level folder, then by extension, then by path. Similar files end up next to each other and compress better.

Extracting without shardkeep:

```
zstd -d -c set1-00000.tar.zst | tar -x
```

## Shard manifest (`<set>-NNNNN.jsonl`)

One JSON line per file, UTF-8:

```json
{"path": "scenes/scene_001/sample.json", "size": 1532, "mtime": 1700000000, "sha256": "9f86d0..."}
```

| Field | Meaning |
|---|---|
| `path` | file path inside the archive and inside the source folder |
| `size` | size in bytes |
| `mtime` | modification time, whole Unix seconds |
| `sha256` | SHA-256 of the file content |

The manifest is written last, after the shard file and the parameters file. A manifest on disk means the shard is complete. Manifest lines stay the same across format versions, and no fields are added to them.

## Shard parameters (`<set>-NNNNN.meta.json`, format 2)

```json
{
 "format": 2,
 "tool": "shardkeep 0.1.0",
 "created": "2026-11-23T12:00:00",
 "file": "set1-00000.tar.zst",
 "codec": "zstd",
 "level": 19,
 "long": true,
 "window_log": 27,
 "threads": 8,
 "files": 1520,
 "raw_bytes": 4294000000,
 "compressed_bytes": 812000000,
 "sha256": "…",
 "skipped": [{"path": "logs/locked.txt", "error": "[Errno 13] Permission denied"}]
}
```

| Field | Meaning |
|---|---|
| `codec`, `level`, `long`, `window_log`, `threads` | the parameters this shard was compressed with |
| `files`, `raw_bytes` | number and total size of files in the shard |
| `compressed_bytes`, `sha256` | size and SHA-256 of the shard file. Verification uses them to catch a shard replaced after packing, and the remote check uses them to confirm an upload without downloading it |
| `skipped` | files from the plan that could not be read. They are missing from the archive, and verification treats this as an error |
| `jxl` | only with `--jxl`: JPEG files converted to JPEG XL, as `{"original path": {"member": "original path.jxl", "size": .jxl size, "sha256": .jxl SHA-256}}` |
| `jxl_fallback` | how many JPEG files were kept as they are: conversion failed, the file was not restored byte for byte, or the .jxl was not smaller |

## JPEG XL files (`pack --jxl`)

With `--jxl`, every JPEG file is converted with `cjxl --lossless_jpeg=1` and immediately restored with `djxl`. The `.jxl` file goes into the archive only if the restored file has the same SHA-256 as the original and the `.jxl` is smaller. Otherwise the original JPEG is stored.

Inside the archive such a file is named `<original path>.jxl`. The manifest keeps the original path, size and SHA-256 of the JPEG file, so `verify --src` and extraction by pattern work with the original names. The name mapping is in the `jxl` field of the shard parameters.

When extracting, shardkeep and the `pack_shards.py` restore script restore the original JPEG with `djxl` and check it against the manifest. Without `djxl`, `--keep-jxl` extracts the `.jxl` files as they are. By hand: `djxl file.jpg.jxl file.jpg`.

## Plan (`plan.jsonl.gz`)

A gzip-compressed list of all files of the set in packing order: `{"s": shard number, "p": path, "n": size, "m": mtime}`. In format 2, already compressed files (photos, video, archives) have `"c": "low"`: they go into separate shards compressed at a low level. The plan is created once, on the first packing run. A resumed run takes the shard contents from it, so files added to the source folder later do not enter the set. `verify --src` reports such files.

## Set parameters (`pack_settings.json`)

```json
{
 "name": "set1", "source": "D:\\data\\set1", "codec": "zstd", "level": 19, "long": true,
 "shard_gb": 4.0, "created": "2026-11-23T12:00:00",
 "format": 2, "tool": "shardkeep 0.1.0", "profile": "auto", "level_low": 1, "window_log": 27,
 "excludes": ["*.tmp"], "include_env": false,
 "sessions": [{"started": "2026-11-24T09:00:00", "level": 22, "long": true, "tool": "shardkeep 0.1.0"}]
}
```

`jxl` is `true` if the set was packed with `--jxl`. The fields on the first line exist in both formats. `level` is the level for well-compressible files on the first run, `level_low` the level for already compressed files (`null` when one level is used for everything), `profile` the compression profile. `excludes` and `include_env` are the file selection rules; `verify --src` uses them to find source files that did not reach the archives. `sessions` lists resumed runs and their parameters.

## Summary (`summary.json`)

Recomputed after every packing run:

| Field | Meaning |
|---|---|
| `name`, `codec`, `level` | set name, codec and level of the first run |
| `shards`, `files`, `raw_bytes`, `compressed_bytes` | finished shards, files and sizes |
| `levels` | all levels the shards were compressed with (format 2) |
| `planned_shards`, `complete` | number of shards in the plan and whether packing finished (format 2) |
| `skipped_files` | how many files could not be read (format 2) |
| `jxl_files`, `jxl_saved_bytes` | how many JPEG files were converted to JPEG XL and how many bytes this saved before compression (only with `--jxl`) |

## Format versions and compatibility

- **Format 1**: archives made by the original `pack_shards.py` script. They have no `.meta.json` files and none of the new fields in `pack_settings.json` and `summary.json`.
- **Format 2**: shardkeep 0.1 and later. Adds shard parameter files and the fields marked above.

Rules kept when the format changes:

1. A new shardkeep version verifies, extracts and resumes sets of all earlier formats.
2. The folder layout, shard names and manifest lines do not change. New information goes into separate files or new JSON fields.
3. The original `pack_shards.py` verifies and extracts format 2 archives: it ignores the extra files and fields. The exception is sets packed with `--jxl`: the original script sees the `.jxl` files as unexpected and the original JPEG files as missing. Such sets need the `pack_shards.py` of the new version (it is placed next to the archives) or shardkeep.
