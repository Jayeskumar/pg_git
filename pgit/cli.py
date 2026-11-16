"""pgit command line: git-like versioned backups of PostgreSQL clusters."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import gc as gcmod
from . import restore as rmod
from .pgutil import basebackup_stream, catalog_maps
from .snapshot import entries_from_dir, entries_from_tar, load_commit, snapshot
from .store import Store


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def _progress(prefix):
    last = [0.0]

    def cb(a, b):
        now = time.time()
        if now - last[0] > 1 and sys.stderr.isatty():
            last[0] = now
            print(f"\r{prefix} {a} / {b}", end="", file=sys.stderr, flush=True)
    return cb


def cmd_init(a):
    s = Store.init(a.repo, zstd_level=a.level, delta=a.delta, max_depth=a.max_depth)
    print(f"initialised pgit repository in {s.path} ({json.dumps(s.config)})")
    s.close()


def cmd_backup(a):
    with Store(a.repo) as s:
        parent = None if a.full else s.head()
        label = a.label or time.strftime("pgit-%Y%m%d-%H%M%S")
        extra = {}
        if a.dbname:
            extra["source"] = {"type": "pg_basebackup"}
            if not a.no_catalog:
                relmap, dbmap = catalog_maps(a.dbname)
                extra["relmap"], extra["dbmap"] = relmap, dbmap
            with basebackup_stream(a.dbname, label=label) as stream:
                cid, commit = snapshot(s, entries_from_tar(stream), parent, label, extra)
        elif a.from_tar:
            extra["source"] = {"type": "tar"}
            f = sys.stdin.buffer if a.from_tar == "-" else open(a.from_tar, "rb")
            cid, commit = snapshot(s, entries_from_tar(f), parent, label, extra)
        elif a.from_dir:
            extra["source"] = {"type": "directory"}
            cid, commit = snapshot(s, entries_from_dir(a.from_dir), parent, label, extra)
        else:
            raise SystemExit("one of --dbname, --from-dir or --from-tar is required")
        if a.tag:
            s.set_ref(a.tag, cid)
        st = commit["stats"]
        if a.json:
            print(json.dumps({"id": cid.hex(), "seq": s.seq_of(cid), **commit["stats"]}))
        else:
            print(f"commit {cid.hex()[:12]}  @{s.seq_of(cid)}  {label}")
            print(f"  {st['files']} files, {human(st['logical_bytes'])} logical, "
                  f"{st['blocks']} blocks: {st['unchanged_blocks']} unchanged, "
                  f"{st['dedup_blocks']} dedup, {st['new_blocks']} new")
            ss = st["store"]
            print(f"  stored {human(ss['new_stored_bytes'])} "
                  f"(objects: " + ", ".join(f"{k}={v}" for k, v in ss["kinds"].items() if v) +
                  f") in {st['seconds']}s")


def cmd_log(a):
    with Store(a.repo) as s:
        refs = {}
        for name, cid in s.refs().items():
            refs.setdefault(cid, []).append(name)
        for seq, cid, _parent, t, label in reversed(s.commits()):
            c = s.get_json(cid)
            st = c["stats"]
            tags = f" ({', '.join(refs[cid])})" if cid in refs else ""
            lsn = c.get("pg", {}).get("start_lsn", "")
            print(f"@{seq:<3} {cid.hex()[:12]} {t} {label or ''}{tags}")
            print(f"      lsn {lsn or '-'}  logical {human(st['logical_bytes'])}  "
                  f"added {human(st['store']['new_stored_bytes'])}  "
                  f"new blocks {st['new_blocks']}/{st['blocks']}")


def cmd_show(a):
    with Store(a.repo) as s:
        cid = s.resolve(a.ref)
        c = load_commit(s, cid)
        if not a.relmap:
            c.pop("relmap", None)
            c.pop("dbmap", None)
        print(json.dumps(c, indent=2))


def cmd_restore(a):
    with Store(a.repo) as s:
        cid = s.resolve(a.ref)
        st = rmod.restore(s, cid, a.target, jobs=a.jobs, delta=a.delta, verify=not a.no_verify,
                          fsync=a.fsync, progress=_progress("segments"))
        if sys.stderr.isatty():
            print(file=sys.stderr)
        if a.json:
            print(json.dumps(st))
        else:
            print(f"restored {cid.hex()[:12]} into {a.target}: {st['files']} files, "
                  f"{human(st['bytes'])}, {st['written_blocks']} blocks written, "
                  f"{st['skipped_blocks']} unchanged, {st['removed']} removed, {st['seconds']}s")


def cmd_verify(a):
    with Store(a.repo) as s:
        refs = [c[1] for c in s.commits()] if a.all else [s.resolve(a.ref)]
        bad = 0
        for cid in refs:
            r = rmod.verify_commit(s, cid, jobs=a.jobs)
            status = "OK" if not r["errors"] else f"{len(r['errors'])} ERRORS"
            print(f"{cid.hex()[:12]}: {r['files']} files {status} ({r['seconds']}s)")
            for e in r["errors"][:20]:
                print("   ", e)
            bad += len(r["errors"])
        sys.exit(1 if bad else 0)


def cmd_fsck(a):
    with Store(a.repo) as s:
        r = rmod.fsck(s, jobs=a.jobs)
        print(f"{r['objects']} objects {r['by_type']} checked in {r['seconds']}s")
        for e in r["errors"][:50]:
            print("  ", e)
        print("OK" if not r["errors"] else f"{len(r['errors'])} problems found")
        sys.exit(1 if r["errors"] else 0)


def cmd_diff(a):
    with Store(a.repo) as s:
        b = s.resolve(a.to)
        if a.frm:
            fa = s.resolve(a.frm)
        else:
            c = s.get_json(b)
            if not c.get("parent"):
                raise SystemExit("commit has no parent; give two revisions")
            fa = bytes.fromhex(c["parent"])
        d = rmod.diff(s, fa, b)
        if a.json:
            print(json.dumps(d, indent=2))
            return
        rows = sorted(d["groups"].items(), key=lambda kv: -kv[1]["bytes_touched"])
        print(f"{'relation / path':<48} {'changed':>8} {'added':>8} {'removed':>8} {'touched':>10}")
        for name, g in rows[: a.limit]:
            print(f"{name[:48]:<48} {g['blocks_changed']:>8} {g['blocks_added']:>8} "
                  f"{g['blocks_removed']:>8} {human(g['bytes_touched']):>10}")
        if len(rows) > a.limit:
            print(f"... {len(rows) - a.limit} more")


def cmd_tag(a):
    with Store(a.repo) as s:
        if a.delete:
            s.del_ref(a.name)
        else:
            s.set_ref(a.name, s.resolve(a.ref))


def cmd_rm(a):
    with Store(a.repo) as s:
        for ref in a.refs:
            cid = s.resolve(ref)
            gcmod.remove_commit(s, cid)
            print(f"removed {cid.hex()[:12]}")
        if a.gc:
            print(json.dumps(gcmod.gc(s), indent=2))


def cmd_gc(a):
    with Store(a.repo) as s:
        r = gcmod.gc(s, dry_run=a.dry_run)
        print(json.dumps(r, indent=2))


def cmd_reindex(a):
    with Store(a.repo) as s:
        print(json.dumps(gcmod.reindex(s), indent=2))


def cmd_stats(a):
    with Store(a.repo) as s:
        r = rmod.repo_stats(s)
        if a.json:
            print(json.dumps(r, indent=2))
            return
        print(f"{len(r['commits'])} commits, {human(r['logical_bytes_all_versions'])} logical, "
              f"{human(r['disk']['total'])} on disk (packs {human(r['disk']['packs'])}, "
              f"index {human(r['disk']['index'])}), ratio {r['ratio']}x")
        for k, v in sorted(r["encodings"].items()):
            print(f"  {k:<18} {v['objects']:>9} objects {human(v['stored_bytes']):>11} stored "
                  f"/ {human(v['raw_bytes']):>11} raw  max depth {v['max_depth']}")
        print("  blob delta depth histogram:", r["blob_delta_depth_histogram"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pgit", description=__doc__)
    p.add_argument("-C", "--repo", default=".", help="repository directory (default: .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("init", help="create a repository")
    x.add_argument("--level", type=int, help="zstd level (default 3)")
    x.add_argument("--delta", choices=["auto", "xor", "zdict", "none"], help="delta encoding")
    x.add_argument("--max-depth", type=int, help="maximum delta chain length (default 8)")
    x.set_defaults(fn=cmd_init)

    x = sub.add_parser("backup", help="take a snapshot")
    x.add_argument("-d", "--dbname", help="libpq connection string (streams pg_basebackup)")
    x.add_argument("--from-dir", help="ingest a stopped data directory / plain base backup")
    x.add_argument("--from-tar", help="ingest a tar base backup ('-' for stdin)")
    x.add_argument("-l", "--label")
    x.add_argument("-t", "--tag")
    x.add_argument("--full", action="store_true", help="do not use the previous snapshot as delta base")
    x.add_argument("--no-catalog", action="store_true", help="skip relation name lookup")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_backup)

    x = sub.add_parser("log", help="list snapshots")
    x.set_defaults(fn=cmd_log)

    x = sub.add_parser("show", help="show a snapshot's metadata")
    x.add_argument("ref", nargs="?", default="HEAD")
    x.add_argument("--relmap", action="store_true")
    x.set_defaults(fn=cmd_show)

    x = sub.add_parser("restore", help="reconstruct a snapshot into a directory")
    x.add_argument("ref")
    x.add_argument("target")
    x.add_argument("-j", "--jobs", type=int, default=4)
    x.add_argument("--delta", action="store_true", help="update an existing directory in place")
    x.add_argument("--fsync", action="store_true")
    x.add_argument("--no-verify", action="store_true", help="skip per-block hash checks")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_restore)

    x = sub.add_parser("verify", help="rebuild snapshot(s) in memory and check file hashes")
    x.add_argument("ref", nargs="?", default="HEAD")
    x.add_argument("--all", action="store_true")
    x.add_argument("-j", "--jobs", type=int, default=4)
    x.set_defaults(fn=cmd_verify)

    x = sub.add_parser("fsck", help="check every object in the repository")
    x.add_argument("-j", "--jobs", type=int, default=4)
    x.set_defaults(fn=cmd_fsck)

    x = sub.add_parser("diff", help="block-level diff between snapshots, by relation")
    x.add_argument("frm", nargs="?", metavar="FROM")
    x.add_argument("to", nargs="?", default="HEAD", metavar="TO")
    x.add_argument("-n", "--limit", type=int, default=25)
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_diff)

    x = sub.add_parser("tag", help="name a snapshot")
    x.add_argument("name")
    x.add_argument("ref", nargs="?", default="HEAD")
    x.add_argument("-d", "--delete", action="store_true")
    x.set_defaults(fn=cmd_tag)

    x = sub.add_parser("rm", help="forget snapshot(s); space is reclaimed by gc")
    x.add_argument("refs", nargs="+")
    x.add_argument("--gc", action="store_true")
    x.set_defaults(fn=cmd_rm)

    x = sub.add_parser("gc", help="repack, dropping unreachable objects")
    x.add_argument("--dry-run", action="store_true")
    x.set_defaults(fn=cmd_gc)

    x = sub.add_parser("reindex", help="rebuild the index from pack files")
    x.set_defaults(fn=cmd_reindex)

    x = sub.add_parser("stats", help="storage statistics")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_stats)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        a.fn(a)
    except (KeyError, FileNotFoundError, FileExistsError) as e:
        print(f"pgit: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
