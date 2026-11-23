[Русский](README.md) | **English**

# shardkeep

shardkeep packs large data folders into compressed shards (tar archives compressed with zstd), verifies the archives against manifests, uploads them to Hugging Face or cloud storage, and restores the data. It is built for hundreds of gigabytes and hundreds of thousands of files: datasets, experiment results, model weights.

- **Shards of about the same size** (4 GB of source data by default). Such files are easy to upload: Hugging Face allows at most 10,000 files per folder, and cloud clients transfer small files very slowly.
- **A manifest for every shard**: path, size, modification time and SHA-256 of each file. The manifests are used to check that the archives are intact and match the source folder.
- **Compression level chosen by content.** Text, JSON and CSV get a high level, while photos, video and archives, which are already compressed, go into separate shards and are compressed quickly.
- **Resuming** after an interrupted packing run or upload.
- **One command before deleting the source data**: it checks the archives, the source folders and the upload.
- **Restoring without shardkeep**: a standalone `pack_shards.py` script is placed next to the archives and needs only Python 3.8+ and the zstandard package. Every shard can also be extracted with the standard `zstd` and `tar` tools.

## Installation

Python 3.10 or later is required. Installing into a separate virtual environment is recommended: huggingface_hub 2.x conflicts with some packages (for example, tokenizers requires a version below 2.0).

Windows (PowerShell):

```powershell
python -m venv $HOME\shardkeep-venv
```

```powershell
& $HOME\shardkeep-venv\Scripts\Activate.ps1
```

```powershell
pip install "shardkeep[hf] @ git+https://github.com/hennessyayden/shardkeep"
```

Linux and macOS:

```bash
python3 -m venv ~/shardkeep-venv && . ~/shardkeep-venv/bin/activate
pip install "shardkeep[hf] @ git+https://github.com/hennessyayden/shardkeep"
```

Without `[hf]`, only packing, verifying and extracting are installed. Uploading through rclone needs the [rclone program](https://rclone.org/install/); JPEG XL needs the `cjxl` and `djxl` programs from [libjxl](https://github.com/libjxl/libjxl/releases).

## Commands

| Command | What it does |
|---|---|
| `shardkeep analyze FOLDER` | analyzes a folder: sizes by folder and file category, duplicates, state of git repositories, compression estimate, recommendations. Read-only |
| `shardkeep pack FOLDER ARCHIVES` | packs a folder into a set of shards |
| `shardkeep verify ARCHIVES` | verifies the archives against their manifests; with `--src`, also checks that the source folder is fully packed and unchanged |
| `shardkeep unpack ARCHIVES DEST` | extracts all sets, one set (`--name`) or files matching a pattern (`--pattern`) |
| `shardkeep upload ARCHIVES --hf USER/DATASET` | uploads to a Hugging Face dataset (private by default) or through rclone (`--rclone yadisk:backup`) |
| `shardkeep check-upload ARCHIVES --hf USER/DATASET` | compares the uploaded files with the local ones by size and SHA-256 without downloading anything |
| `shardkeep check ARCHIVES --hf USER/DATASET` | every check that matters before deleting the source data, with a final verdict |
| `shardkeep card ARCHIVES` | creates or updates the dataset card `README.md` |
| `shardkeep compare-zip ARCHIVE.zip FOLDER` | checks that a zip archive matches an extracted folder |

Full help: `shardkeep <command> --help`.

## Scenarios

In all examples `D:\work\project` is the working folder and `E:\backup\dataset` is the archive folder on another disk. Windows commands are given one per block: multi-line commands pasted into PowerShell are sometimes joined into one line.

### Free up disk space

1. Analyze the working folder to see what takes up space:

   ```powershell
   shardkeep analyze "D:\work\project" --out "D:\project_report"
   ```

   `D:\project_report\report.md` lists sizes by folder and category, duplicates, git repositories with uncommitted changes and a compression estimate. Environments and caches can be deleted right away; code belongs in git.

2. Pack the data folders (each folder becomes a set):

   ```powershell
   shardkeep pack "D:\work\project\data" "E:\backup\dataset"
   ```

   ```powershell
   shardkeep pack "D:\work\project\results" "E:\backup\dataset"
   ```

3. Upload and check before deleting (see the next scenario). Delete the source folders only after the `SAFE TO DELETE` verdict.

### Back up a dataset to Hugging Face

1. Log in to Hugging Face (once):

   ```powershell
   hf auth login
   ```

2. Pack the data and give the repository name for the dataset card:

   ```powershell
   shardkeep pack "D:\work\project\data" "E:\backup\dataset" --hf-repo user/project-data --title "Project data"
   ```

3. Upload (a private dataset is created automatically):

   ```powershell
   shardkeep upload "E:\backup\dataset" --hf user/project-data
   ```

   If the upload is interrupted, the same command resumes it: files that are already uploaded are skipped.

4. Check everything before deleting the source data:

   ```powershell
   shardkeep check "E:\backup\dataset" --hf user/project-data
   ```

   The command checks the content of every shard against its manifest, compares every source folder (remembered during packing) with the archives, and compares the uploaded files by SHA-256. The result is written to `check_report.txt`.

### Restore

With shardkeep installed:

```powershell
hf download user/project-data --repo-type dataset --local-dir "D:\restore\backup"
```

```powershell
shardkeep unpack "D:\restore\backup" "D:\restore\data"
```

Without shardkeep, with Python and zstandard only:

```powershell
pip install zstandard huggingface_hub
```

```powershell
python "D:\restore\backup\pack_shards.py" verify "D:\restore\backup"
```

```powershell
python "D:\restore\backup\pack_shards.py" unpack "D:\restore\backup" "D:\restore\data"
```

One set: `--name <set>`. Single files: `--pattern "scenes/scene_001/*"`. To download one set only: `hf download ... --include "data/<set>/*" --include "manifest/<set>/*" --include pack_shards.py`. Without Python, a shard is extracted with `zstd -d -c <shard>.tar.zst | tar -x`.

### Cloud storage through rclone

Configure the storage once with `rclone config` (for example, named `yadisk` for Yandex Disk):

```powershell
shardkeep upload "E:\backup\dataset" --rclone yadisk:backup/dataset
```

```powershell
shardkeep check "E:\backup\dataset" --rclone yadisk:backup/dataset
```

SHA-256 is compared where the storage provides it (Yandex Disk, Google Drive). Other files are compared by size, and the program reports how many files were checked by size only.

### Check a zip archive before deleting the extracted folder

```powershell
shardkeep compare-zip "D:\work\old_results.zip" "D:\work\old_results" --crc --test
```

`--crc` compares the CRC32 checksums stored in the zip, so the archive is not extracted. `--test` also checks the integrity of the archive.

## Compression profiles

| Profile | What it does |
|---|---|
| `auto` (default) | test-compresses a sample of files for every extension. Well-compressible files get zstd 19, already compressed ones (reduced by less than 10%) get zstd 1 in separate shards |
| `media` | the same, but decided by extension without reading files: photos, video, archives and Office documents get zstd 1 |
| `max` | zstd 22 for all files. Slow, and wastes hours on already compressed data |
| `normal` | zstd 19 for all files, as in the original `pack_shards.py` |
| `fast` | zstd 3 without long-distance matching |

`--level` sets the level for well-compressible files in any profile, for example `--profile auto --level 22`. Long-distance matching (128 MB window) is on everywhere except `fast` and shards of already compressed files. Any zstd program decompresses shards with this window without extra options.

**Memory.** At levels 19-22 with long-distance matching, every zstd thread takes 1-2 GB of memory. The number of threads is chosen automatically from the free memory. An explicit `--threads` is respected, with a warning if memory may run out.

**Resuming.** Running the same `pack` command again continues from the saved plan: finished shards are skipped. Options that are not given are taken from the saved settings. If another level is given when resuming, the program warns, and the level of every shard is recorded next to its manifest.

## Dataset card

`pack` creates `README.md` in the archive folder only if it does not exist yet. You can edit the card by hand. `shardkeep card` updates only the sets table and the restore commands in it: they sit between the `<!-- shardkeep:begin sets -->` and `<!-- shardkeep:end sets -->` markers (and `restore`). A card without markers is left unchanged. `--force` rewrites it from the template and keeps the old one as `README.md.bak`.

License and metadata:

```powershell
shardkeep card "E:\backup\dataset" --license other --license-name my-license --license-link https://example.org/terms --license-file "D:\work\license.txt" --tag robotics
```

The license text is taken from the file as is. An example for Waymo Open Dataset data: [examples/waymo-license.md](examples/waymo-license.md). The card language is chosen with `--lang en` or `--lang ru`. A custom card template is set with `--template`; it can use `{{title}}`, `{{description}}`, `{{yaml}}`, `{{sets}}`, `{{restore}}`, `{{license_section}}`, `{{hf_repo}}` and `{{github_url}}`. Settings are remembered in `card_settings.json`. `GITHUB_README_SNIPPET.md` next to the card is a snippet for the README of the code repository.

## Analyzer rules

File categories and classification rules are defined in [src/shardkeep/analyze/rules.toml](src/shardkeep/analyze/rules.toml). Your own rules file is checked before the built-in one:

```toml
[[category]]
id = "annotations"
title = "Annotations"
data = true
advice = "Keep them together with the images."

[[rule]]
category = "annotations"
ext = [".txt"]
dir = ["labels"]

[[notice]]
path = ["*waymo*"]
text = "The project contains Waymo Open Dataset data: it may be shared only with registered users."
```

```powershell
shardkeep analyze "D:\work\project" --rules "D:\work\my_rules.toml"
```

A rule applies when all its conditions hold: `ext` (extensions), `not_ext`, `name` (file name pattern), `path` (path pattern), `dir` and `not_dir` (name pattern of any parent folder), `min_size` and `max_size` (for example `"50MB"`). `[[notice]]` rules add a notice to the report when a matching path is found.

## JPEG XL

With `--jxl`, JPEG files are converted to JPEG XL losslessly (`cjxl --lossless_jpeg=1`). A file usually becomes about 20% smaller, and the original JPEG is restored byte for byte. Every file is checked right during packing. If the conversion fails or the file is not restored byte for byte, the original JPEG is stored.

```powershell
shardkeep pack "D:\work\photos" "E:\backup\photos" --jxl
```

Extracting needs the `djxl` program. Without it, `unpack --keep-jxl` extracts the `.jxl` files as they are.

## Archive format

```
E:\backup\dataset\
  README.md                          dataset card
  pack_shards.py                     restore script
  data\<set>\<set>-00000.tar.zst     shards
  manifest\<set>\<set>-00000.jsonl       shard manifest
  manifest\<set>\<set>-00000.meta.json   shard parameters
  manifest\<set>\plan.jsonl.gz, pack_settings.json, summary.json
```

Full description: [docs/format.en.md](docs/format.en.md).

**Compatibility with the original `pack_shards.py`.** shardkeep verifies, extracts and resumes archives made by the original script. The original script verifies and extracts shardkeep archives, except sets packed with `--jxl`. Commands from already published dataset cards (`python pack_shards.py verify ...`) work with the new restore script.

## Windows notes

- Paths longer than 260 characters are supported when packing, verifying and extracting.
- Rules, license and template files are read as UTF-8, UTF-8 with BOM and UTF-16, so files written by `>` in PowerShell 5.1 (UTF-16) or by `Set-Content -Encoding utf8` (with BOM) work. The program itself writes UTF-8 without BOM, except the analyzer CSV tables: they have a BOM so that Excel opens them in the right encoding. For Excel with a comma as the decimal separator, use `--csv-delimiter ";"`.
- The analyzer inspects git repositories in folders owned by another user (it works around git's "dubious ownership" refusal).
- A dataset card saved in Notepad with CRLF line endings is updated by `card` without changing its line endings.

## Limitations

- Only regular files are stored. Symbolic links and junctions are skipped, empty folders are not kept.
- Modification times are kept to the second. Permissions, owners and attributes are not kept; extracted files get mode `0644`.
- The packing plan is made on the first run. Files added later do not enter the started set. `verify --src` or `check` reports them, and they need a new set.
- A file that cannot be read during packing (no access, locked by another program) is left out and recorded in the shard parameters. Verification treats this as an error unless `--allow-skipped` is given.
- If a file changes during packing, the archive gets its content at the moment it was read. `verify --src` reports that the file has changed.
- xz compression is single-threaded and several times slower than zstd. It is used only if zstd is unavailable or explicitly chosen.
- The rclone check compares SHA-256 only where the storage provides it.
- Sets with JPEG XL cannot be extracted by the original `pack_shards.py`, and restoring the JPEG files needs `djxl`.

## Development

```powershell
pip install -e ".[dev,hf]"
```

```powershell
python -m pytest
```

The tests use small synthetic data and never touch the network: Hugging Face, rclone, cjxl and djxl are replaced with fakes. GitHub Actions runs them on Windows and Linux with Python 3.10-3.14 and checks the restore script on Python 3.8.

## License

MIT, see [LICENSE](LICENSE).
