#!/usr/bin/env python3
"""Turn bench/results/results.json into docs/RESULTS.md plus SVG charts."""
from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# categorical slots (fixed order) - light / dark steps, validated palette
SLOTS = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
         ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#0ca30c"),
         ("#6250d6", "#9085e9"), ("#e34948", "#e66767")]

MAIN_TOOLS = ["pgit", "pgBackRest (block incr, zst)", "pgBackRest (file incr, zst)",
              "restic (on plain basebackup)", "git (on plain basebackup)",
              "pg_basebackup (full, zstd)", "pg_dump (logical, zstd)"]
SHORT = {"pgit": "pgit", "pgBackRest (block incr, zst)": "pgBackRest block-incr",
         "pgBackRest (file incr, zst)": "pgBackRest file-incr",
         "restic (on plain basebackup)": "restic", "git (on plain basebackup)": "git",
         "pg_basebackup (full, zstd)": "pg_basebackup (full)",
         "pg_dump (logical, zstd)": "pg_dump (logical)"}


def mib(n):
    return f"{n / 2**20:,.1f} MiB" if n is not None else "–"


def secs(x):
    return f"{x:.2f} s" if isinstance(x, (int, float)) else "–"


def yes(b):
    return {True: "✅", False: "❌", None: "–"}.get(b, "–")


# ------------------------------------------------------------------- SVG
def _style(n_series):
    light = "".join(f"--s{i + 1}:{SLOTS[i][0]};" for i in range(n_series))
    dark = "".join(f"--s{i + 1}:{SLOTS[i][1]};" for i in range(n_series))
    return f"""<style>
svg{{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;{light}}}
@media (prefers-color-scheme: dark){{svg{{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;{dark}}}}}
text{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;fill:var(--ink2);font-size:12px}}
.t{{font-size:15px;font-weight:600;fill:var(--ink)}} .m{{fill:var(--muted);font-size:11px}}
.g{{stroke:var(--grid);stroke-width:1}} .a{{stroke:var(--axis);stroke-width:1}}
</style>"""


def _nice_max(v):
    import math
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= v:
            return m * e
    return 10 * e


def line_chart(series, title, ylabel, yfmt, xlabel="version", width=760, height=400):
    """series: list of (name, [(x, y)])  - colors by fixed slot order."""
    series = [s for s in series if s[1]] or [("no data", [(1, 0)])]
    L, R, T, B = 70, 190, 44, 48
    pw, ph = width - L - R, height - T - B
    xs = sorted({x for _n, pts in series for x, _y in pts})
    ymax = _nice_max(max(y for _n, pts in series for _x, y in pts))
    x0, x1 = xs[0], xs[-1]

    def sx(x):
        return L + (x - x0) / max(1, x1 - x0) * pw

    def sy(y):
        return T + ph - y / ymax * ph

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'width="{width}" height="{height}" role="img" aria-label="{title}">', _style(len(series)),
           f'<rect width="{width}" height="{height}" fill="var(--bg)" rx="8"/>',
           f'<text class="t" x="{L}" y="24">{title}</text>']
    for k in range(6):
        y = ymax * k / 5
        out.append(f'<line class="g" x1="{L}" x2="{L + pw}" y1="{sy(y):.1f}" y2="{sy(y):.1f}"/>')
        out.append(f'<text class="m" x="{L - 8}" y="{sy(y) + 4:.1f}" text-anchor="end">{yfmt(y)}</text>')
    out.append(f'<line class="a" x1="{L}" x2="{L + pw}" y1="{T + ph}" y2="{T + ph}"/>')
    for x in xs:
        out.append(f'<text class="m" x="{sx(x):.1f}" y="{T + ph + 18}" text-anchor="middle">{x}</text>')
    out.append(f'<text class="m" x="{L + pw / 2}" y="{height - 10}" text-anchor="middle">{xlabel}</text>')
    out.append(f'<text class="m" transform="translate(16 {T + ph / 2}) rotate(-90)" '
               f'text-anchor="middle">{ylabel}</text>')
    for i, (name, pts) in enumerate(series):
        c = f"var(--s{i + 1})"
        d = " ".join(f"{'M' if j == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for j, (x, y) in enumerate(pts))
        out.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in pts:
            out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4" fill="{c}" stroke="var(--bg)" '
                       f'stroke-width="2"><title>{name} v{x}: {yfmt(y)}</title></circle>')
    # legend (right), in series order
    for i, (name, pts) in enumerate(series):
        ly = T + 8 + i * 22
        out.append(f'<line x1="{L + pw + 18}" x2="{L + pw + 38}" y1="{ly}" y2="{ly}" '
                   f'stroke="var(--s{i + 1})" stroke-width="3" stroke-linecap="round"/>')
        out.append(f'<text x="{L + pw + 44}" y="{ly + 4}">{name}</text>')
    out.append("</svg>")
    return "\n".join(out)


def hbar_chart(rows, segs, title, xfmt, width=760, row_h=30):
    """rows: list of (label, [v_seg1, v_seg2, ...]); stacked horizontal bars."""
    L, R, T = 190, 80, 64
    height = T + len(rows) * row_h + 40
    pw = width - L - R
    xmax = _nice_max(max(sum(v) for _l, v in rows))

    def sx(x):
        return L + x / xmax * pw

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'width="{width}" height="{height}" role="img" aria-label="{title}">', _style(len(segs)),
           f'<rect width="{width}" height="{height}" fill="var(--bg)" rx="8"/>',
           f'<text class="t" x="16" y="24">{title}</text>']
    lx = 16
    for i, s in enumerate(segs):
        out.append(f'<rect x="{lx}" y="38" width="12" height="12" rx="3" fill="var(--s{i + 1})"/>')
        out.append(f'<text x="{lx + 18}" y="48">{s}</text>')
        lx += 30 + 7.5 * len(s)
    for k in range(6):
        x = xmax * k / 5
        out.append(f'<line class="g" x1="{sx(x):.1f}" x2="{sx(x):.1f}" y1="{T - 4}" '
                   f'y2="{T + len(rows) * row_h}"/>')
        out.append(f'<text class="m" x="{sx(x):.1f}" y="{T + len(rows) * row_h + 16}" '
                   f'text-anchor="middle">{xfmt(x)}</text>')
    for r, (label, vals) in enumerate(rows):
        y = T + r * row_h + 6
        h = row_h - 12
        out.append(f'<text x="{L - 10}" y="{y + h / 2 + 4}" text-anchor="end">{label}</text>')
        x = 0.0
        for i, v in enumerate(vals):
            if v <= 0:
                continue
            w = max(1.0, sx(x + v) - sx(x) - (2 if i < len(vals) - 1 else 0))
            last = i == len(vals) - 1 or all(vv <= 0 for vv in vals[i + 1:])
            rx = 4 if last else 0
            out.append(f'<rect x="{sx(x):.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="{rx}" '
                       f'fill="var(--s{i + 1})"><title>{label} – {segs[i]}: {xfmt(v)}</title></rect>')
            x += v
        out.append(f'<text x="{sx(x) + 6:.1f}" y="{y + h / 2 + 4}" style="fill:var(--ink)">'
                   f'{xfmt(sum(vals))}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- report
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "bench" / "results" / "results.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "RESULTS.md"))
    a = ap.parse_args()
    r = json.load(open(a.results))
    out_dir = Path(a.out).parent
    img = out_dir / "img"
    img.mkdir(parents=True, exist_ok=True)
    vers = r["versions"]
    N = len(vers)
    tools = [t for t in MAIN_TOOLS if t in vers[0]["backups"]]
    cfg = r["config"]

    def size_at(t, i):
        b = vers[i]["backups"].get(t, {})
        return b.get("repo_bytes")

    md = []
    md.append("# Benchmark results\n")
    md.append(f"Generated by `bench/report.py` from `bench/results/results.json`. "
              f"PostgreSQL {r['pg_version']}, pgbench scale {cfg['scale']} "
              f"(cluster ≈ {vers[0]['db_bytes'] / 2**20:.0f} MiB at v1, "
              f"{vers[-1]['db_bytes'] / 2**20:.0f} MiB at v{N}), {N} versions, "
              f"{os.cpu_count()} vCPU / Linux {platform.release().split('-')[0]}; every tool "
              f"used zstd level 3 and, where it supports it, 4 parallel processes. "
              f"Caches were warm (a single container), so the times are comparisons "
              f"between tools, not absolute figures.\n")
    md.append("## Workload\n")
    md.append("| version | change applied before the backup | cluster size |\n|---:|---|---:|")
    for e in vers:
        md.append(f"| v{e['v']} | {e['step']} | {mib(e['db_bytes'])} |")
    md.append("")

    # ---------------------------------------------------------- storage
    logical = sum(e["db_bytes"] for e in vers)
    bb_total = size_at("pg_basebackup (full, zstd)", N - 1)
    md.append("## Storage\n")
    md.append(f"Total repository size after storing all {N} versions. *Logical* is the sum "
              f"of the cluster sizes over all versions ({mib(logical)}), meaning what {N} "
              f"uncompressed copies would take.\n")
    md.append("| tool | first backup | mean added per later version | total after "
              f"v{N} | vs. logical | vs. full pg_basebackup |\n|---|---:|---:|---:|---:|---:|")
    rows = []
    for t in tools:
        first = size_at(t, 0)
        total = size_at(t, N - 1)
        if first is None or total is None:
            md.append(f"| {SHORT[t]} | error | | | | |")
            continue
        inc = (total - first) / max(1, N - 1)
        rows.append((t, first, inc, total))
        note = ""
        if t.startswith("git") and r.get("git_gc", {}).get("repo_bytes"):
            note = f" ({mib(r['git_gc']['repo_bytes'])} after `git gc`)"
        md.append(f"| {SHORT[t]} | {mib(first)} | {mib(inc)} | {mib(total)}{note} | "
                  f"{logical / total:.1f}× | " + ("baseline" if t.startswith("pg_basebackup") else
                                          f"{bb_total / total:.1f}× smaller") + " |")
    md.append("")
    br = [t for t in tools if t.startswith("pgBackRest")]
    if br:
        arch = vers[-1]["backups"][br[0]].get("wal_archive")
        md.append(f"pgBackRest's sizes are the `backup/` directory only. It ran with "
                  f"`archive-copy=y`, so each backup carries the WAL it needs to be consistent, "
                  f"the same as `pg_basebackup -X fetch` and pgit. Its continuous WAL archive "
                  f"(another {mib(arch)}) buys point-in-time recovery, which the other tools "
                  f"do not offer, and is left out.\n")
    series = [(SHORT[t], [(e["v"], e["backups"][t]["repo_bytes"] / 2**20) for e in vers
                          if "repo_bytes" in e["backups"].get(t, {})]) for t in tools]
    (img / "storage.svg").write_text(line_chart(series, "Repository size after each version",
                                                "MiB", lambda y: f"{y:,.0f}"))
    md.append("![Repository size after each version](img/storage.svg)\n")
    inc_tools = [t for t in tools if t in ("pgit", "pgBackRest (block incr, zst)",
                                           "pgBackRest (file incr, zst)",
                                           "restic (on plain basebackup)")]
    series = []
    for t in inc_tools:
        pts = []
        for i in range(1, N):
            a_, b_ = size_at(t, i - 1), size_at(t, i)
            if a_ is not None and b_ is not None:
                pts.append((vers[i]["v"], (b_ - a_) / 2**20))
        series.append((SHORT[t], pts))
    (img / "incremental.svg").write_text(line_chart(series, "Bytes added by each incremental version",
                                                    "MiB added", lambda y: f"{y:,.0f}"))
    md.append("![Bytes added per version](img/incremental.svg)\n")

    md.append("### Bytes added per version\n")
    md.append("| version | change | " + " | ".join(SHORT[t] for t in inc_tools) + " |\n|---:|---|"
              + "---:|" * len(inc_tools))
    for i in range(1, N):
        cells = []
        for t in inc_tools:
            a_, b_ = size_at(t, i - 1), size_at(t, i)
            cells.append(mib(b_ - a_) if a_ is not None and b_ is not None else "–")
        md.append(f"| v{vers[i]['v']} | {vers[i]['step']} | " + " | ".join(cells) + " |")
    md.append("")

    # pgit variants
    var = [t for t in vers[0]["backups"] if t.startswith("pgit-")]
    if var:
        md.append("### pgit encoding variants (same tar streams, ingested with `--from-tar`)\n")
        md.append("| variant | delta encoding | total after v%d | added after v1 |\n|---|---|---:|---:|" % N)
        desc = {"pgit-dedup-only": "none: changed pages stored whole (dedup + dictionary only)",
                "pgit-xor-only": "XOR against the previous version of the page",
                "pgit-zdict-only": "zstd with the previous page as dictionary",
                "pgit-auto-from-tar": "auto: smaller of XOR / zdict (default)",
                "pgit-auto-depth2": "auto, delta chains capped at 2"}
        for t in var:
            f0, fN = size_at(t, 0), size_at(t, N - 1)
            if f0 is None or fN is None:
                continue
            md.append(f"| `{t}` | {desc.get(t, '')} | {mib(fN)} | {mib(fN - f0)} |")
        md.append("")

    ps = r.get("pgit_stats")
    if ps:
        md.append("### Where pgit's bytes go\n")
        md.append(f"Packs {mib(ps['disk']['packs'])} + index {mib(ps['disk']['index'])}; "
                  f"{mib(ps['logical_bytes_all_versions'])} logical across all versions "
                  f"(physical backups including WAL), a **{ps['ratio']}×** reduction.\n")
        md.append("| object / encoding | objects | stored | raw | max delta depth |\n|---|---:|---:|---:|---:|")
        for k, v in sorted(ps["encodings"].items(), key=lambda kv: -kv[1]["stored_bytes"]):
            md.append(f"| {k} | {v['objects']:,} | {mib(v['stored_bytes'])} | {mib(v['raw_bytes'])} "
                      f"| {v['max_depth']} |")
        md.append("")

    # ------------------------------------------------------------ backup time
    md.append("## Backup time\n")
    md.append("| tool | v1 (full) | mean of later versions | slowest |\n|---|---:|---:|---:|")
    for t in tools:
        ts = [e["backups"][t]["seconds"] for e in vers if "seconds" in e["backups"].get(t, {})]
        if not ts:
            continue
        md.append(f"| {SHORT[t]} | {secs(ts[0])} | {secs(sum(ts[1:]) / max(1, len(ts) - 1))} "
                  f"| {secs(max(ts))} |")
    st = [e.get("stage_seconds") for e in vers if e.get("stage_seconds")]
    if st:
        md.append(f"\nrestic and git back up a plain `pg_basebackup -Fp` copy. Making that copy "
                  f"took {secs(sum(st) / len(st))} per version on average and is **not** included "
                  f"above.")
    if r.get("git_gc", {}).get("seconds"):
        md.append(f" A final `git gc` (delta compression) took {secs(r['git_gc']['seconds'])}.")
    md.append("\n")

    # --------------------------------------------------------------- restores
    md.append("## Restore time and integrity of restored clusters\n")
    md.append("Each restore was followed by starting PostgreSQL on the result. Startup runs "
              "crash recovery, or archive recovery for pgBackRest, until it accepts connections. "
              "Then the server computed an order-independent hash of every row of every table. "
              "*Match* means the hash equals the one recorded on the primary when that version "
              "was backed up.\n")
    md.append("| tool | version | restore | startup + recovery | total | match | pg_verifybackup "
              "| pg_amcheck (heapallindexed) |\n|---|---:|---:|---:|---:|:-:|:-:|:-:|")
    res = r["restores"]
    for rec in res:
        if rec["tool"] not in SHORT:
            continue
        if rec["tool"] == "pgit" and rec["v"] not in (1, (N + 1) // 2, N):
            continue
        tot = (rec.get("restore_seconds") or 0) + (rec.get("startup_seconds") or 0)
        md.append(f"| {SHORT[rec['tool']]} | v{rec['v']} | {secs(rec.get('restore_seconds'))} | "
                  f"{secs(rec.get('startup_seconds'))} | {secs(tot)} | "
                  f"{yes(rec.get('checksums_match'))} | {yes(rec.get('pg_verifybackup'))} | "
                  f"{yes(rec.get('pg_amcheck'))} |" + (f" error: {rec['error'][:80]}" if 'error' in rec else ""))
    md.append("")
    last = [rec for rec in res if rec["v"] == N and rec["tool"] in SHORT and "restore_seconds" in rec]
    order = [t for t in tools]
    last.sort(key=lambda rec: order.index(rec["tool"]))
    (img / "restore.svg").write_text(hbar_chart(
        [(SHORT[rec["tool"]], [rec["restore_seconds"], rec.get("startup_seconds") or 0]) for rec in last],
        ["restore files", "startup + recovery"], f"Time to a running cluster, latest version (v{N})",
        lambda x: f"{x:.1f} s"))
    md.append(f"![Restore time of v{N}](img/restore.svg)\n")
    pg = [rec for rec in res if rec["tool"] == "pgit"]
    if pg:
        md.append("### pgit restore of every version\n")
        md.append("Restoring a later version follows longer delta chains (capped by `max_depth`, "
                  "8 by default).\n")
        md.append("| version | restore | startup | match | pg_verifybackup | pg_checksums |\n"
                  "|---:|---:|---:|:-:|:-:|:-:|")
        for rec in pg:
            md.append(f"| v{rec['v']} | {secs(rec.get('restore_seconds'))} | "
                      f"{secs(rec.get('startup_seconds'))} | {yes(rec.get('checksums_match'))} | "
                      f"{yes(rec.get('pg_verifybackup'))} | {yes(rec.get('pg_checksums'))} |")
        md.append("")
    dr = r.get("pgit_delta_restore")
    if dr:
        a1, a2 = dr["last_to_first"], dr["first_to_last"]
        md.append("### In-place (`--delta`) restore\n")
        md.append(f"`pgit restore --delta` rewrites only the blocks that differ in an existing "
                  f"directory, similar to `git checkout` or `pgbackrest restore --delta`.\n")
        md.append("| switch | time | blocks written | blocks unchanged | files removed | match |\n"
                  "|---|---:|---:|---:|---:|:-:|")
        md.append(f"| v{N} → v1 | {secs(a1['seconds'])} | {a1['written_blocks']:,} | "
                  f"{a1['skipped_blocks']:,} | {a1['removed']} | {yes(a1['checksums_match'])} |")
        md.append(f"| v1 → v{N} | {secs(a2['seconds'])} | {a2['written_blocks']:,} | "
                  f"{a2['skipped_blocks']:,} | {a2['removed']} | {yes(a2['checksums_match'])} |")
        md.append("")

    # -------------------------------------------------------------- integrity
    md.append("## Integrity checking of the repository itself\n")
    if r.get("pgit_fsck_seconds") is not None:
        md.append(f"`pgit fsck` (decode and hash every object, then check reachability) took "
                  f"{secs(r['pgit_fsck_seconds'])}; `pgit verify --all` (rebuild every file of all "
                  f"{N} versions in memory and compare whole-file SHA-256) took "
                  f"{secs(r['pgit_verify_all_seconds'])}.\n")
    md.append("One byte was flipped in the middle of each tool's largest repository file (a copy), "
              "and the tool's own verification command was run on it:\n")
    md.append("| tool | detected | how | versions affected |\n|---|:-:|---|---:|")
    for t, c in r.get("corruption", {}).items():
        if t not in SHORT:
            continue
        md.append(f"| {SHORT[t]} | {yes(c.get('detected'))} | {c.get('how', c.get('error', ''))} "
                  f"| {c.get('versions_affected', '–')} |")
    md.append("")
    md.append("*Versions affected* counts the snapshots the tool reported as damaged. It depends on "
              "*which* file was hit, so it is not a like-for-like comparison. For pgBackRest the "
              "largest file happened to belong to the newest incremental. For pgit it was the first "
              "pack, which holds pages that every later snapshot shares (dedup) or builds deltas on. "
              "That is the inherent trade-off of deduplicated and incremental storage: a damaged "
              "pgBackRest *full* backup also invalidates the incrementals that depend on it. pgit "
              "detects such damage on every read and names the affected objects, but it cannot "
              "repair from a single copy, so keep the repository on redundant storage.\n")

    # ------------------------------------------------------------------- diff
    d = r.get("pgit_diff_first_last")
    if d:
        md.append(f"## `pgit diff v1 v{N}`: what changed, by relation\n")
        md.append("| relation | blocks changed | added | removed | touched |\n|---|---:|---:|---:|---:|")
        groups = sorted(d["groups"].items(), key=lambda kv: -kv[1]["bytes_touched"])[:12]
        for name, g in groups:
            md.append(f"| `{name}` | {g['blocks_changed']:,} | {g['blocks_added']:,} | "
                      f"{g['blocks_removed']:,} | {mib(g['bytes_touched'])} |")
        md.append("")

    Path(a.out).write_text("\n".join(md) + "\n")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
