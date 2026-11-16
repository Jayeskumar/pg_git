"""Removing snapshots and repacking (garbage collection)."""
from __future__ import annotations

import os
import time

from .snapshot import file_block_ids
from .store import OT_COMMIT, ObjMeta, Store


def reachable(store: Store) -> set[bytes]:
    live: set[bytes] = set()
    for _seq, cid, _p, _t, _l in store.commits():
        live.add(cid)
        c = store.get_json(cid)
        tid = bytes.fromhex(c["tree"])
        live.add(tid)
        tree = store.get_json(tid)
        for e in tree["files"]:
            if e["t"] != "f":
                continue
            for h in e.get("bl", []):
                live.add(bytes.fromhex(h))
            live.update(file_block_ids(store, e))
    return live


def remove_commit(store: Store, cid: bytes):
    store.db.execute("DELETE FROM commits WHERE id=?", (cid,))
    store.db.execute("DELETE FROM refs WHERE id=?", (cid,))


def gc(store: Store, dry_run: bool = False) -> dict:
    """Rewrite all packs keeping only reachable objects.

    Objects whose delta base became unreachable are materialised and re-encoded
    as full objects (deltas refer to raw content, so all other encodings stay
    valid and are copied verbatim).
    """
    t0 = time.time()
    store.finish_pack()
    store.preload()
    old = dict(store._mem_index)
    live = reachable(store)
    before = store.disk_usage()
    dead = [k for k in old if k not in live]
    result = {"objects_before": len(old), "objects_live": len(live), "objects_dropped": len(dead),
              "disk_before": before}
    if dry_run or not dead:
        result["seconds"] = round(time.time() - t0, 3)
        result["disk_after"] = before
        return result

    old_packs = store.pack_numbers()
    new_meta: dict[bytes, ObjMeta] = {}
    rebased = 0
    # bases always have a smaller depth than their dependants
    order = sorted((k for k in live if k in old),
                   key=lambda k: (old[k].depth, old[k].pack, old[k].off))
    for id_ in order:
        m = old.get(id_)
        if m is None:
            continue  # reported by fsck
        if m.base is not None and m.base in live and m.base in new_meta:
            payload = store.read_payload(m)
            pack, off = store._append(m.otype, m.kind, m.rawlen, payload, m.base)
            depth = new_meta[m.base].depth + 1
            new_meta[id_] = ObjMeta(m.otype, m.kind, pack, off, len(payload), m.rawlen, m.base, depth)
        elif m.base is None:
            payload = store.read_payload(m)
            pack, off = store._append(m.otype, m.kind, m.rawlen, payload, None)
            new_meta[id_] = ObjMeta(m.otype, m.kind, pack, off, len(payload), m.rawlen, None, 0)
        else:
            # base is gone: re-delta against the nearest surviving ancestor in the
            # old chain (e.g. the page two snapshots back), else store it full
            raw = store.get(id_)
            anc = old[m.base].base if m.base in old else None
            while anc is not None and anc not in new_meta:
                anc = old[anc].base if anc in old else None
            anc_raw = None
            if anc is not None and new_meta[anc].depth < store.config["max_depth"]:
                anc_raw = store.get(anc)
            kind, payload, used = store._encode(raw, anc, anc_raw, store.config["delta"]
                                                if anc_raw is not None else "none")
            pack, off = store._append(m.otype, kind, m.rawlen, payload, used)
            depth = new_meta[used].depth + 1 if used else 0
            new_meta[id_] = ObjMeta(m.otype, kind, pack, off, len(payload), m.rawlen, used, depth)
            rebased += 1
    if store._wfile is not None:
        store._wfile.flush()
        os.fsync(store._wfile.fileno())
        store._wfile.close()
        store._wfile = None
        store._wpack = None

    db = store.db
    db.execute("BEGIN")
    db.execute("DELETE FROM objects")
    db.executemany(
        "INSERT INTO objects (id, otype, kind, pack, off, len, rawlen, base, depth)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(k, m.otype, m.kind, m.pack, m.off, m.len, m.rawlen, m.base, m.depth)
         for k, m in new_meta.items()])
    db.execute("COMMIT")
    for fd in store._fds.values():
        os.close(fd)
    store._fds.clear()
    for n in old_packs:
        os.unlink(store._pack_path(n))
    store._mem_index = new_meta
    store._cache.clear()
    db.execute("VACUUM")
    result.update({"re_encoded": rebased, "disk_after": store.disk_usage(),
                   "seconds": round(time.time() - t0, 3)})
    return result


def reindex(store: Store) -> dict:
    """Rebuild index.sqlite (objects + commit list) from the pack files alone.

    Bases are always written before the objects that depend on them, so a
    sequential scan can decode (and hash) every record.
    """
    import json

    from .store import IntegrityError, oid

    t0 = time.time()
    store.finish_pack()
    store._mem_index = {}
    store._cache.clear()
    bad = 0
    commits = []
    for n in store.pack_numbers():
        for otype, kind, rawlen, off, paylen, base in store.scan_pack(n):
            depth = 0
            if base is not None:
                bm = store._mem_index.get(base)
                depth = bm.depth + 1 if bm else 0
            m = ObjMeta(otype, kind, n, off, paylen, rawlen, base, depth)
            # decode with a placeholder id, then compute the real id from content
            tmp = b"\0" * 31 + b"\1"
            store._mem_index[tmp] = m
            try:
                raw = store.get(tmp, verify=False)
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            finally:
                store._mem_index.pop(tmp, None)
                store._cache.pop(tmp, None)
            id_ = oid(raw)
            store._mem_index[id_] = m
            if otype == OT_COMMIT:
                c = json.loads(raw)
                commits.append((c["time"], id_, c.get("parent"), c.get("label")))
    db = store.db
    db.execute("BEGIN")
    db.execute("DELETE FROM objects")
    db.executemany(
        "INSERT OR IGNORE INTO objects (id, otype, kind, pack, off, len, rawlen, base, depth)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(k, m.otype, m.kind, m.pack, m.off, m.len, m.rawlen, m.base, m.depth)
         for k, m in store._mem_index.items()])
    known = {row[0] for row in db.execute("SELECT id FROM commits")}
    added = 0
    # order by time; ties broken by ancestry (parents first)
    parents = {cid: (bytes.fromhex(p) if p else None) for _t, cid, p, _l in commits}

    def chain(cid):
        n = 0
        while parents.get(cid) in parents and n < len(parents):
            cid = parents[cid]
            n += 1
        return n

    for t, cid, parent, label in sorted(commits, key=lambda c: (c[0], chain(c[1]))):
        if cid not in known:
            db.execute("INSERT INTO commits (id, parent, time, label) VALUES (?,?,?,?)",
                       (cid, bytes.fromhex(parent) if parent else None, t, label))
            added += 1
    db.execute("COMMIT")
    if bad:
        raise IntegrityError(f"{bad} undecodable records found while reindexing")
    return {"objects": len(store._mem_index), "commits_found": len(commits),
            "commits_added": added, "seconds": round(time.time() - t0, 3)}
