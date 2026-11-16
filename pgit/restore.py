"""Reconstructing a snapshot into a directory, verification and diffs."""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from .snapshot import file_block_ids, load_commit, load_tree, relation_key
from .store import KIND_NAMES, OTYPE_NAMES, Store

SEGMENT_BLOCKS = 1024   # unit of parallel work: 8 MiB of a file


def restore(store: Store, cid: bytes, target: str, jobs: int = 4, delta: bool = False,
            verify: bool = True, fsync: bool = False, progress=None) -> dict:
    """Write the snapshot ``cid`` into ``target``.

    With ``delta=True`` an existing directory is updated in place: only blocks
    whose content differs are rewritten, files that are not part of the
    snapshot are removed (like ``git checkout`` / ``pgbackrest restore --delta``).
    """
    t0 = time.time()
    store.preload()
    commit = load_commit(store, cid)
    tree = load_tree(store, commit)
    bs = tree["block_size"]
    target = os.path.abspath(target)

    if os.path.exists(target) and not delta:
        if os.listdir(target):
            raise FileExistsError(f"{target} is not empty (use --delta to update in place)")
    os.makedirs(target, mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)

    wanted = {e["p"] for e in tree["files"]}
    removed = 0
    if delta:
        removed = _prune_extra(target, wanted, {e["p"]: e["t"] for e in tree["files"]})

    stats = {"files": 0, "blocks": 0, "bytes": 0, "written_blocks": 0, "skipped_blocks": 0,
             "removed": removed}
    tasks = []
    dirs = []
    for e in tree["files"]:
        path = os.path.join(target, e["p"])
        if e["t"] == "d":
            if os.path.islink(path) or (os.path.exists(path) and not os.path.isdir(path)):
                os.unlink(path)
            os.makedirs(path, exist_ok=True)
            dirs.append((path, e["m"]))
        elif e["t"] == "l":
            if os.path.lexists(path):
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(e["target"], path)
        else:
            ids = file_block_ids(store, e)
            size = e["s"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exists = False
            if os.path.lexists(path):
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                elif os.path.islink(path):
                    os.unlink(path)
                else:
                    exists = delta
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | (0 if exists else os.O_TRUNC), 0o600)
            try:
                os.ftruncate(fd, size)
            finally:
                os.close(fd)
            os.chmod(path, e["m"])
            stats["files"] += 1
            stats["blocks"] += len(ids)
            stats["bytes"] += size
            for k in range(0, len(ids), SEGMENT_BLOCKS):
                tasks.append((path, k, ids[k:k + SEGMENT_BLOCKS], exists))

    def work(task):
        path, start, ids, exists = task
        fd = os.open(path, os.O_RDWR)
        written = skipped = 0
        try:
            if exists:
                cur = os.pread(fd, len(ids) * bs, start * bs)
            for j, bid in enumerate(ids):
                off = (start + j) * bs
                if exists:
                    old = cur[j * bs:(j + 1) * bs]
                    if old and hashlib.sha256(old).digest() == bid:
                        skipped += 1
                        continue
                data = store.get(bid, verify=verify)
                os.pwrite(fd, data, off)
                written += 1
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        return written, skipped

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for w, s in ex.map(work, tasks):
            stats["written_blocks"] += w
            stats["skipped_blocks"] += s
            done += 1
            if progress:
                progress(done, len(tasks))

    # directory permissions last (PostgreSQL insists on 0700/0750)
    for path, mode in reversed(dirs):
        os.chmod(path, mode)
    if fsync:
        _fsync_tree(target)
    stats["seconds"] = round(time.time() - t0, 3)
    stats["commit"] = cid.hex()
    return stats


def _prune_extra(target: str, wanted: set[str], types: dict[str, str]) -> int:
    removed = 0
    for dirpath, dirnames, filenames in os.walk(target, topdown=True):
        rel_dir = os.path.relpath(dirpath, target)
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in list(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(dirpath, name)
            if rel not in wanted or types.get(rel) != "d" or os.path.islink(full):
                if os.path.islink(full):
                    os.unlink(full)
                else:
                    shutil.rmtree(full)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel not in wanted or types.get(rel) == "d":
                os.unlink(os.path.join(dirpath, name))
                removed += 1
    return removed


def _fsync_tree(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            fd = os.open(p, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        fd = os.open(dirpath, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)


# ---------------------------------------------------------------- verification
def verify_commit(store: Store, cid: bytes, jobs: int = 4) -> dict:
    """Rebuild every file of a commit in memory and compare with its SHA-256."""
    t0 = time.time()
    store.preload()
    commit = load_commit(store, cid)
    tree = load_tree(store, commit)
    files = [e for e in tree["files"] if e["t"] == "f"]
    errors = []

    def check(e):
        try:
            h = hashlib.sha256()
            n = 0
            for bid in file_block_ids(store, e):
                data = store.get(bid)
                h.update(data)
                n += len(data)
            if n != e["s"] or h.hexdigest() != e["h"]:
                return f"{e['p']}: content mismatch"
        except Exception as ex:  # noqa: BLE001
            return f"{e['p']}: {ex}"
        return None

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for r in ex.map(check, files):
            if r:
                errors.append(r)
    return {"commit": cid.hex(), "files": len(files), "errors": errors,
            "seconds": round(time.time() - t0, 3)}


def fsck(store: Store, jobs: int = 4) -> dict:
    """Decode every object in every pack and check its hash; check reachability."""
    t0 = time.time()
    store.preload()
    idx = store._mem_index
    errors = []
    counts = {}

    def check(item):
        id_, m = item
        try:
            store.get(id_, verify=True)
        except Exception as ex:  # noqa: BLE001
            return f"object {id_.hex()} ({OTYPE_NAMES.get(m.otype)}): {ex}"
        return None

    for m in idx.values():
        counts[OTYPE_NAMES[m.otype]] = counts.get(OTYPE_NAMES[m.otype], 0) + 1
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for r in ex.map(check, idx.items(), chunksize=256):
            if r:
                errors.append(r)
    # reachability: every commit's tree, blocklists and blocks must exist
    missing = 0
    for _seq, cid, _p, _t, _l in store.commits():
        if cid not in idx:
            errors.append(f"commit {cid.hex()} missing")
            continue
        try:
            c = store.get_json(cid)
            tree = store.get_json(bytes.fromhex(c["tree"]))
            for e in tree["files"]:
                if e["t"] != "f":
                    continue
                for h in e.get("bl", []):
                    if bytes.fromhex(h) not in idx:
                        missing += 1
                for bid in file_block_ids(store, e):
                    if bid not in idx:
                        missing += 1
        except Exception as ex:  # noqa: BLE001
            errors.append(f"commit {cid.hex()}: {ex}")
    if missing:
        errors.append(f"{missing} referenced objects missing")
    return {"objects": len(idx), "by_type": counts, "errors": errors,
            "seconds": round(time.time() - t0, 3)}


# ------------------------------------------------------------------------ diff
def diff(store: Store, a: bytes, b: bytes) -> dict:
    """Block-level difference between two commits, grouped by relation."""
    ca, cb = load_commit(store, a), load_commit(store, b)
    ta = {e["p"]: e for e in load_tree(store, ca)["files"]}
    tb = {e["p"]: e for e in load_tree(store, cb)["files"]}
    bs = 8192
    relmap = {**ca.get("relmap", {}), **cb.get("relmap", {})}
    dbmap = {**ca.get("dbmap", {}), **cb.get("dbmap", {})}
    groups: dict[str, dict] = {}

    def group_name(path):
        rk = relation_key(path)
        if rk:
            rel, fork = rk
            name = relmap.get(rel)
            if name is None:
                parts = rel.split("/")
                db = dbmap.get(parts[1], parts[1]) if parts[0] == "base" else parts[0]
                name = f"{db}:relfilenode {parts[-1]}"
            return name if fork == "main" else f"{name} [{fork}]"
        if path.startswith("pg_wal/"):
            return "pg_wal"
        top = path.split("/")[0]
        return top if "/" in path else path

    for p in sorted(set(ta) | set(tb)):
        ea, eb = ta.get(p), tb.get(p)
        if ea and eb and ea.get("t") == eb.get("t") == "f" and ea["h"] == eb["h"]:
            continue
        if ea and eb and ea.get("t") != "f" and ea == eb:
            continue
        g = groups.setdefault(group_name(p), {"files_added": 0, "files_removed": 0,
                                              "files_changed": 0, "blocks_changed": 0,
                                              "blocks_added": 0, "blocks_removed": 0})
        if ea is None:
            g["files_added"] += 1
            if eb["t"] == "f":
                g["blocks_added"] += len(file_block_ids(store, eb))
        elif eb is None:
            g["files_removed"] += 1
            if ea["t"] == "f":
                g["blocks_removed"] += len(file_block_ids(store, ea))
        elif ea["t"] == eb["t"] == "f":
            g["files_changed"] += 1
            ia, ib = file_block_ids(store, ea), file_block_ids(store, eb)
            common = min(len(ia), len(ib))
            g["blocks_changed"] += sum(1 for k in range(common) if ia[k] != ib[k])
            g["blocks_added"] += max(0, len(ib) - len(ia))
            g["blocks_removed"] += max(0, len(ia) - len(ib))
        else:
            g["files_changed"] += 1
    for g in groups.values():
        g["bytes_touched"] = (g["blocks_changed"] + g["blocks_added"]) * bs
    return {"from": a.hex(), "to": b.hex(), "groups": groups}


# ----------------------------------------------------------------- statistics
def repo_stats(store: Store) -> dict:
    kinds = {}
    for otype, kind, n, stored, raw, maxd in store.db.execute(
            "SELECT otype, kind, count(*), sum(len), sum(rawlen), max(depth) "
            "FROM objects GROUP BY otype, kind"):
        kinds[f"{OTYPE_NAMES[otype]}/{KIND_NAMES[kind]}"] = {
            "objects": n, "stored_bytes": stored, "raw_bytes": raw, "max_depth": maxd}
    depth_hist = dict(store.db.execute(
        "SELECT depth, count(*) FROM objects WHERE otype=0 GROUP BY depth ORDER BY depth"))
    logical = 0
    commits = []
    for seq, cid, _p, t, label in store.commits():
        c = store.get_json(cid)
        lb = c["stats"]["logical_bytes"]
        logical += lb
        commits.append({"seq": seq, "id": cid.hex(), "time": t, "label": label,
                        "logical_bytes": lb,
                        "added_bytes": c["stats"]["store"]["new_stored_bytes"]})
    du = store.disk_usage()
    return {"commits": commits, "logical_bytes_all_versions": logical, "disk": du,
            "ratio": round(logical / du["total"], 2) if du["total"] else None,
            "encodings": kinds, "blob_delta_depth_histogram": depth_hist}
