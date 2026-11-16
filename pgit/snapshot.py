"""Building snapshots (commits) from a PostgreSQL base backup, and reading them back."""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import io
import os
import re
import stat
import struct
import tarfile
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator

from .store import OT_BLIST, OT_BLOB, OT_TREE, Store

INLINE_BLOCKS = 8          # files with <= this many blocks keep ids inline in the tree
IDS_PER_BLIST = 256        # 256 * 32 B = one 8 KiB blocklist chunk
DEFAULT_EXCLUDES = ["postmaster.pid", "postmaster.opts"]


# --------------------------------------------------------------------- entries
@dataclass
class Entry:
    path: str
    type: str                 # 'f' file, 'd' directory, 'l' symlink
    mode: int
    size: int = 0
    target: str | None = None
    fileobj: BinaryIO | None = None
    mtime: int = 0


def _norm(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def entries_from_tar(fileobj: BinaryIO) -> Iterator[Entry]:
    """Stream entries out of a tar archive (e.g. ``pg_basebackup -Ft -D -``)."""
    with tarfile.open(fileobj=fileobj, mode="r|*") as tf:
        for m in tf:
            p = _norm(m.name)
            if not p or p == ".":
                continue
            if m.isdir():
                yield Entry(p, "d", m.mode & 0o7777, mtime=int(m.mtime))
            elif m.issym():
                yield Entry(p, "l", m.mode & 0o7777, target=m.linkname, mtime=int(m.mtime))
            elif m.isreg():
                yield Entry(p, "f", m.mode & 0o7777, m.size, fileobj=tf.extractfile(m),
                            mtime=int(m.mtime))
            # hard links / devices do not appear in PostgreSQL data directories


def entries_from_dir(root: str, excludes: list[str] | None = None) -> Iterator[Entry]:
    """Walk a (stopped, or pg_basebackup -Fp produced) data directory."""
    excludes = DEFAULT_EXCLUDES if excludes is None else excludes
    root = os.path.abspath(root)

    def excluded(rel):
        return any(fnmatch.fnmatch(rel, pat) for pat in excludes)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in list(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if excluded(rel):
                dirnames.remove(name)
                continue
            if stat.S_ISLNK(st.st_mode):
                dirnames.remove(name)
                yield Entry(rel, "l", 0o777, target=os.readlink(full), mtime=int(st.st_mtime))
            else:
                yield Entry(rel, "d", stat.S_IMODE(st.st_mode), mtime=int(st.st_mtime))
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if excluded(rel):
                continue
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                yield Entry(rel, "l", 0o777, target=os.readlink(full), mtime=int(st.st_mtime))
            elif stat.S_ISREG(st.st_mode):
                f = open(full, "rb")
                try:
                    yield Entry(rel, "f", stat.S_IMODE(st.st_mode), st.st_size, fileobj=f,
                                mtime=int(st.st_mtime))
                finally:
                    f.close()


# ------------------------------------------------------------------ tree utils
def file_block_ids(store: Store, entry: dict) -> list[bytes]:
    """Return the list of block ids of a tree file entry."""
    if "b" in entry:
        return [bytes.fromhex(h) for h in entry["b"]]
    out: list[bytes] = []
    for h in entry.get("bl", []):
        raw = store.get(bytes.fromhex(h))
        out.extend(raw[i:i + 32] for i in range(0, len(raw), 32))
    return out


def load_commit(store: Store, cid: bytes) -> dict:
    c = store.get_json(cid)
    c["id"] = cid.hex()
    return c


def load_tree(store: Store, commit: dict) -> dict:
    return store.get_json(bytes.fromhex(commit["tree"]))


def tree_index(tree: dict) -> dict[str, dict]:
    return {e["p"]: e for e in tree["files"]}


# ----------------------------------------------------------- pg metadata parse
def parse_backup_label(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower().replace(" ", "_")
        v = v.strip()
        if k == "start_wal_location":
            m = re.match(r"(\S+) \(file (\S+)\)", v)
            if m:
                out["start_lsn"], out["start_wal_file"] = m.group(1), m.group(2)
                continue
        out[k] = v
    return out


def parse_pg_control(data: bytes) -> dict:
    if len(data) < 16:
        return {}
    sysid, ctl_ver, cat_ver = struct.unpack_from("<QII", data, 0)
    return {"system_identifier": str(sysid), "pg_control_version": ctl_ver,
            "catalog_version": cat_ver}


_REL_RE = re.compile(r"^(base/\d+|global|pg_tblspc/\d+/[^/]+/\d+)/(\d+)(_fsm|_vm|_init)?(\.\d+)?$")


def relation_key(path: str) -> tuple[str, str] | None:
    """'base/5/16384_fsm.2' -> ('base/5/16384', 'fsm')."""
    m = _REL_RE.match(path)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}", (m.group(3) or "_main")[1:]


# ------------------------------------------------------------------- snapshot
@dataclass
class SnapshotStats:
    files: int = 0
    dirs: int = 0
    blocks: int = 0
    logical_bytes: int = 0
    unchanged_blocks: int = 0      # same id as the same position in the parent
    dedup_blocks: int = 0          # new at this position but already in the store
    new_blocks: int = 0
    seconds: float = 0.0
    store: dict = field(default_factory=dict)

    def as_dict(self):
        d = dict(self.__dict__)
        return d


def snapshot(store: Store, entries: Iterator[Entry], parent: bytes | None = None,
             label: str | None = None, extra: dict | None = None,
             progress=None) -> tuple[bytes, dict]:
    """Ingest ``entries`` into ``store`` as a new commit whose parent is ``parent``."""
    t0 = time.time()
    bs = store.block_size
    store.stats_reset()
    st = SnapshotStats()
    parent_tree_id = None
    parent_files: dict[str, dict] = {}
    if parent is not None:
        pc = load_commit(store, parent)
        parent_tree_id = bytes.fromhex(pc["tree"])
        parent_files = tree_index(load_tree(store, pc))

    files = []
    pg = {}
    for e in entries:
        if e.type == "d":
            files.append({"p": e.path, "t": "d", "m": e.mode})
            st.dirs += 1
            continue
        if e.type == "l":
            files.append({"p": e.path, "t": "l", "m": e.mode, "target": e.target})
            continue
        pe = parent_files.get(e.path)
        pblocks = file_block_ids(store, pe) if pe and pe.get("t") == "f" else []
        fh = hashlib.sha256()
        ids: list[bytes] = []
        size = 0
        i = 0
        f = e.fileobj
        small = bytearray()
        while True:
            chunk = f.read(bs)
            if not chunk:
                break
            # tar streams may return short reads; normalise to full blocks
            while len(chunk) < bs:
                more = f.read(bs - len(chunk))
                if not more:
                    break
                chunk += more
            size += len(chunk)
            fh.update(chunk)
            if e.path in ("backup_label", "global/pg_control") and len(small) < 65536:
                small += chunk
            bid = hashlib.sha256(chunk).digest()
            if i < len(pblocks) and pblocks[i] == bid:
                st.unchanged_blocks += 1
            elif store.has(bid):
                st.dedup_blocks += 1
            else:
                store.put(chunk, OT_BLOB, base=pblocks[i] if i < len(pblocks) else None, id_=bid)
                st.new_blocks += 1
            ids.append(bid)
            i += 1
            if progress and (st.blocks + i) % 16384 == 0:
                progress(st.blocks + i, st.logical_bytes + size)
        if e.path == "backup_label":
            pg.update(parse_backup_label(small.decode(errors="replace")))
        elif e.path == "global/pg_control":
            pg.update(parse_pg_control(bytes(small)))
        ent = {"p": e.path, "t": "f", "m": e.mode, "s": size, "h": fh.hexdigest()}
        if len(ids) <= INLINE_BLOCKS:
            ent["b"] = [x.hex() for x in ids]
        else:
            pbl = pe.get("bl", []) if pe else []
            bl = []
            for k in range(0, len(ids), IDS_PER_BLIST):
                raw = b"".join(ids[k:k + IDS_PER_BLIST])
                j = k // IDS_PER_BLIST
                base = bytes.fromhex(pbl[j]) if j < len(pbl) else None
                bl.append(store.put(raw, OT_BLIST, base=base).hex())
            ent["bl"] = bl
            ent["n"] = len(ids)
        files.append(ent)
        st.files += 1
        st.blocks += len(ids)
        st.logical_bytes += size

    tree = {"block_size": bs, "files": files}
    tree_id = store.put_json(tree, OT_TREE, base=parent_tree_id)
    st.seconds = round(time.time() - t0, 3)
    st.store = dict(store.stats)
    commit = {
        "tree": tree_id.hex(),
        "parent": parent.hex() if parent else None,
        "time": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "pg": pg,
        "stats": st.as_dict(),
    }
    if extra:
        commit.update(extra)
    cid = store.add_commit(commit)
    return cid, commit


# -------------------------------------------------------------------- helpers
def read_all(f: BinaryIO) -> bytes:
    buf = io.BytesIO()
    while True:
        b = f.read(1 << 20)
        if not b:
            return buf.getvalue()
        buf.write(b)
