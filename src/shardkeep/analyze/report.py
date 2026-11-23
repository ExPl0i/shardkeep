"""Отчет shardkeep analyze: report.md, summary.json и таблицы CSV."""

import csv
import datetime as dt
import json
import os
import re
from collections import defaultdict

from shardkeep.analyze.scan import ROOT_FILES_KEY, compressor_name, ratio_for
from shardkeep.fsutil import write_text
from shardkeep.textfmt import FILES, human, num, pct, plural, ts_local

TEMP_NAME_RE = re.compile(r"(^|[_\-\s])(output|outputs|test|testing|tmp|temp|shown|cache|debug|vis|viz)([_\-\s]|$)",
                          re.IGNORECASE)
MANY_SMALL = 10_000


def _title(rules, cat):
    c = rules.categories.get(cat)
    return c.title if c else cat


def unit_advice(rules, unit, st, dup_in_unit, small_limit):
    lines = []
    for cat, b in sorted(st.cat_bytes.items(), key=lambda x: -x[1]):
        if b < max(st.bytes * 0.01, 10 * 2**20) and lines:
            continue
        c = rules.categories.get(cat)
        advice = c.advice if c else ""
        n = st.cat_files[cat]
        lines.append(f"**{_title(rules, cat)}** ({human(b)}, {num(n)} {plural(n, FILES)}): {advice}")
    if st.small_files >= MANY_SMALL:
        lines.append(f"Many small files (under {human(small_limit)}): {num(st.small_files)}. Pack them into shards "
                     "before uploading: Hugging Face allows at most 10,000 files per folder and recommends at most "
                     "100,000 files per repository, and cloud clients transfer small files very slowly.")
    if dup_in_unit:
        lines.append(f"Duplicates inside this folder or with other folders: up to {human(dup_in_unit)} can be freed "
                     "(see duplicates.csv).")
    if TEMP_NAME_RE.search(unit):
        lines.append("The folder name suggests temporary or intermediate results: check whether they are needed "
                     "and whether they can be recreated.")
    return lines


def repo_advice(r):
    if r["error"] and not r["remotes"] and r["modified"] is None:
        return f"could not be checked ({r['error'][:150]})."
    parts = []
    outside = (r["worktree_bytes"] - r["tracked_bytes"]) if r["tracked_bytes"] is not None else 0
    if not r["remotes"]:
        parts.append("no remote repository: create one (for example on GitHub) and run "
                     "`git remote add origin <url>` and `git push -u origin --all`.")
    else:
        url = next(iter(r["remotes"].values()))
        if (r["modified"] or 0) > 0 or (r["untracked"] or 0) > 0:
            parts.append(f"uncommitted changes ({r['modified'] or 0} modified, {r['untracked'] or 0} untracked): "
                         "commit what you need and push it; for someone else's repository, keep your changes in a "
                         "fork or with `git diff > my_changes.patch`.")
        if (r["unpushed"] or 0) > 0:
            parts.append(f"{r['unpushed']} commits are not pushed: run `git push --all`.")
        if not parts:
            parts.append(f"everything is on the remote, the folder can be restored with `git clone {url}`.")
    if outside > 50 * 2**20:
        parts.append(f"{human(outside)} inside the folder are not tracked by git (data, weights, results): handle "
                     "them separately, or they are lost when the folder is deleted.")
    if r["lfs"]:
        parts.append("Git LFS is used: make sure large files are pushed (`git lfs push --all origin`).")
    return " ".join(x[:1].upper() + x[1:] for x in parts)


def write_csv(path, header, rows, delimiter):
    # utf-8-sig: Excel открывает такие файлы в правильной кодировке
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def build_report(out, opts, sc, dups, repos, per_group, per_cat, npz, elapsed):
    os.makedirs(out, exist_ok=True)
    rules = sc.rules
    root_st = sc.nodes[()]
    total = root_st.bytes
    units = sorted([k for k in sc.nodes if len(k) == 1], key=lambda k: -sc.nodes[k].bytes)
    sample_counts = {k: len(v) for k, v in sc.samples.items()}
    delim = opts.get("csv_delimiter", ",")

    dup_by_unit = defaultdict(int)
    dup_pairs = defaultdict(int)
    dup_total = 0
    for g in dups:
        size, paths = g["size"], g["paths"]
        dup_total += size * (len(paths) - 1)
        us = [p.split("/")[0] if "/" in p else ROOT_FILES_KEY for p in paths]
        for u in us[1:]:
            dup_by_unit[u] += size
        for i in range(len(us)):
            for j in range(i + 1, len(us)):
                if us[i] != us[j]:
                    dup_pairs[tuple(sorted((us[i], us[j])))] += size

    est_by_unit = {}
    est_by_cat = defaultdict(float)
    for k in units:
        e = 0.0
        for cat, b in sc.nodes[k].cat_bytes.items():
            r = ratio_for(k[0], cat, per_group, per_cat, sample_counts) if per_cat else 1.0
            e += b * r
            est_by_cat[cat] += b * r
        est_by_unit[k[0]] = e

    have_est = bool(per_cat)
    md = [f"# Analysis of {sc.root}\n",
          f"Date: {dt.datetime.now():%Y-%m-%d %H:%M}. Analysis took {elapsed / 60:.1f} min.\n",
          f"Total: **{human(total)}**, {num(root_st.files)} files, {num(root_st.dirs)} folders. "
          f"Access errors: {sc.error_count}. Symbolic links and junctions skipped: {sc.skipped_links}.\n"]
    if have_est:
        md.append(f"Estimated size after compression ({compressor_name()}): **{human(sum(est_by_unit.values()))}**. "
                  "The estimate is based on a sample of files and may differ from the real result by 10-20%.\n")

    md += ["## Size by category\n", "| Category | Size | Share | Files | After compression (estimate) |",
           "|---|---:|---:|---:|---:|"]
    for cat, b in sorted(root_st.cat_bytes.items(), key=lambda x: -x[1]):
        est = human(est_by_cat[cat]) if have_est else "-"
        md.append(f"| {_title(rules, cat)} | {human(b)} | {pct(b, total)} | {num(root_st.cat_files[cat])} | {est} |")
    md.append("")

    md += ["## Top-level folders\n",
           "| Folder | Size | Share | Files | Small files | Main category | After compression (estimate) | Modified |",
           "|---|---:|---:|---:|---:|---|---:|---|"]
    for k in units:
        st = sc.nodes[k]
        main_cat = max(st.cat_bytes.items(), key=lambda x: x[1])[0] if st.cat_bytes else "other"
        est = human(est_by_unit.get(k[0], 0)) if have_est else "-"
        md.append(f"| {k[0]} | {human(st.bytes)} | {pct(st.bytes, total)} | {num(st.files)} | {num(st.small_files)} | "
                  f"{_title(rules, main_cat)} | {est} | {ts_local(st.mtime_max)} |")
    md.append("")

    md.append("## Recommendations by folder\n")
    for k in units:
        st = sc.nodes[k]
        if st.bytes < opts["min_report_mb"] * 2**20:
            continue
        md.append(f"### {k[0]}: {human(st.bytes)}\n")
        top_ext = sorted(st.ext_bytes.items(), key=lambda x: -x[1])[:8]
        md.append("Extensions: " + ", ".join(f"{e} {human(b)} ({num(st.ext_files[e])})" for e, b in top_ext) + ".\n")
        subs = sorted([s for s in sc.nodes if len(s) == 2 and s[0] == k[0]], key=lambda s: -sc.nodes[s].bytes)
        if subs:
            md.append("Largest subfolders: " + ", ".join(
                f"{s[1]} {human(sc.nodes[s].bytes)} ({num(sc.nodes[s].files)} {plural(sc.nodes[s].files, FILES)})"
                for s in subs[:8]) + ".\n")
        for line in unit_advice(rules, k[0], st, dup_by_unit.get(k[0], 0), opts["small_kb"] * 1024):
            md.append(f"- {line}")
        md.append("")

    if npz.get("checked"):
        s, d = npz["stored_bytes"], npz["deflated_bytes"]
        md += ["## npz files\n",
               f"npz files in the sample: {npz['checked']}. Arrays stored without compression: {human(s)}, "
               f"compressed: {human(d)}."]
        if s > d:
            md.append("Most arrays were saved with np.savez without compression, so packing with zstd will reduce "
                      "them a lot. If your data loader allows it, you can also re-save them with "
                      "np.savez_compressed, or convert floating-point arrays to float16 (this changes the "
                      "precision of the data).\n")
        else:
            md.append("The arrays are already compressed (np.savez_compressed); an archiver will gain little.\n")

    md.append("## Duplicates\n")
    if opts["dup_mode"] == "off":
        md.append("Duplicate search is disabled.\n")
    elif not dups:
        md.append(f"No identical files of {opts['dup_min_mb']} MB or larger were found.\n")
    else:
        kind = "full hash match" if opts["dup_mode"] == "full" else "partial hash match, needs checking"
        md.append(f"Groups of identical files: {len(dups)} ({kind}). Up to **{human(dup_total)}** can be freed.\n")
        if dup_pairs:
            md += ["| Folders | Duplicated size |", "|---|---:|"]
            for (a, b), v in sorted(dup_pairs.items(), key=lambda x: -x[1])[:15]:
                md.append(f"| {a} and {b} | {human(v)} |")
            md.append("")
        md.append("Largest groups:\n")
        for g in dups[:10]:
            md.append(f"- {human(g['size'])} x {len(g['paths'])}: " + "; ".join(g["paths"][:4])
                      + (" ..." if len(g["paths"]) > 4 else ""))
        md.append("")

    md.append("## Git repositories\n")
    if opts["no_git"]:
        md.append("Git check is disabled.\n")
    elif not repos:
        md.append("No git repositories found. Put the project code into a git repository before deleting anything.\n")
    else:
        md += ["| Repository | Remote | Modified | Untracked | Unpushed commits | Outside git | Last commit |",
               "|---|---|---:|---:|---:|---:|---|"]
        for r in repos:
            remote = ", ".join(r["remotes"].values()) or "none"
            outside = (r["worktree_bytes"] - r["tracked_bytes"]) if r["tracked_bytes"] is not None else None

            def q(v):
                return "?" if v is None else v
            md.append(f"| {r['path']} | {remote} | {q(r['modified'])} | {q(r['untracked'])} | {q(r['unpushed'])} | "
                      f"{human(outside) if outside is not None else '?'} | {r['last_commit'][:10]} |")
        md.append("")
        for r in repos:
            md.append(f"- **{r['path']}**: " + repo_advice(r))
        md.append("")

    hits = [n for n in rules.notices if n.hit]
    if hits:
        md.append("## Notices\n")
        for n in hits:
            md.append(f"- {n.text}")
        md.append("")

    data_cats = {c for c, cat in rules.categories.items() if cat.data}
    md.append("## Next steps\n")
    if have_est:
        data_est = sum(v for c, v in est_by_cat.items() if c in data_cats)
        md.append(f"Data, weights and logs take about **{human(data_est)}** after compression (environments, code "
                  "and git internals are not included). Compare this with the limits of your storage plan.\n")
    md += ["1. Make sure all code is in remote git repositories (no uncommitted changes or unpushed commits, see above).",
           "2. Delete what can be recreated: environments and caches, raw data that can be downloaded again, "
           "unchanged clones of other people's repositories.",
           "3. Delete duplicates, keeping one copy.",
           "4. Pack the remaining data, final weights and logs with `shardkeep pack`, check them with "
           "`shardkeep verify --src`, and only then delete the source folders.",
           "5. Upload the archives with `shardkeep upload`, check the upload with `shardkeep check-upload`, and add a "
           "link to them to the README of your code repository.\n"]

    if sc.errors:
        md.append("## Access errors (first 30)\n")
        md += [f"- {e}" for e in sc.errors[:30]]
        md.append("")

    write_text(os.path.join(out, "report.md"), "\n".join(md))

    cats = list(rules.categories)
    rows = []
    for k in sorted(sc.nodes, key=lambda k: (len(k), -sc.nodes[k].bytes)):
        if not k:
            continue
        st = sc.nodes[k]
        rows.append(["/".join(k), len(k), st.bytes, round(st.bytes / 2**30, 3), st.files, st.small_files,
                     ts_local(st.mtime_min), ts_local(st.mtime_max)] + [st.cat_bytes.get(c, 0) for c in cats])
    write_csv(os.path.join(out, "folders.csv"),
              ["folder", "level", "bytes", "GB", "files", "small files", "oldest", "newest"]
              + [f"bytes: {c}" for c in cats], rows, delim)

    rows = []
    for k in sorted(sc.nodes, key=lambda k: (len(k), -sc.nodes[k].bytes)):
        if len(k) > 1:
            continue
        st = sc.nodes[k]
        for e, b in sorted(st.ext_bytes.items(), key=lambda x: -x[1]):
            rows.append(["/".join(k) or ".", e, b, round(b / 2**30, 3), st.ext_files[e]])
    write_csv(os.path.join(out, "extensions.csv"), ["folder", "extension", "bytes", "GB", "files"], rows, delim)
    write_csv(os.path.join(out, "largest_files.csv"), ["file", "bytes", "GB", "category"],
              [[p, s, round(s / 2**30, 3), c] for s, p, c in sorted(sc.largest, reverse=True)], delim)
    rows = [[i, p, g["size"], g["hash"], "yes" if g["verified"] else "no"]
            for i, g in enumerate(dups, 1) for p in g["paths"]]
    write_csv(os.path.join(out, "duplicates.csv"), ["group", "file", "bytes", "hash", "fully verified"], rows, delim)
    write_csv(os.path.join(out, "git_repos.csv"),
              ["repository", "remote", "branch", "modified", "untracked", "unpushed", "worktree bytes",
               "tracked bytes", ".git bytes", "lfs", "last commit", "error"],
              [[r["path"], " ".join(r["remotes"].values()), r["branch"], r["modified"], r["untracked"], r["unpushed"],
                r["worktree_bytes"], r["tracked_bytes"], r["git_bytes"], r["lfs"], r["last_commit"], r["error"]]
               for r in repos], delim)

    summary = {
        "root": sc.root, "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "compressor_for_estimate": compressor_name() if have_est else None,
        "categories": {c: {"title": cat.title, "data": cat.data} for c, cat in rules.categories.items()},
        "total": root_st.to_dict(),
        "folders": {"/".join(k): sc.nodes[k].to_dict() for k in sc.nodes if k},
        "estimated_compressed_bytes": {u: int(v) for u, v in est_by_unit.items()} if have_est else None,
        "compression_ratio_by_category": per_cat,
        "npz": npz, "duplicates_reclaimable_bytes": dup_total, "duplicate_groups": len(dups),
        "git_repos": repos, "notices": [n.text for n in hits],
        "errors": sc.errors, "error_count": sc.error_count,
    }
    write_text(os.path.join(out, "summary.json"), json.dumps(summary, ensure_ascii=False, indent=1, default=str))
