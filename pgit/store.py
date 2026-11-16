"""Content-addressed object store with page-level delta compression.

Layout of a repository directory::

    repo/
      config.json          repository settings (block size, zstd level, delta policy)
      index.sqlite         object index, commit list and refs
      packs/pack-NNNNNN.pack   append-only pack files

Every object is identified by the SHA-256 of its *raw* content, so identical
pages are stored once no matter which file, fork or version they come from
(deduplication).  How an object is *encoded* inside a pack is independent of
its identity, exactly like git blobs vs. packfile deltas:

* ``RAW``    payload is the raw bytes (used when compression does not help)
* ``ZSTD``   payload is zstd(raw)
* ``XOR``    payload is zstd(raw XOR base), base has the same length
* ``ZDICT``  payload is zstd(raw) compressed with ``base`` as a raw-content
             dictionary (handles shifted tuples / line pointers in B-tree pages)
* ``TDICT``  payload is zstd(raw) compressed with a zstd dictionary trained on
             the repository's first pages; ``base`` is the id of that
             dictionary object.  This recovers most of the ratio that is lost
             by compressing 8 KiB pages independently.

Deltas are taken against the raw content of the base, never against its
encoding, so a base can be re-encoded (e.g. by ``gc``) without invalidating
the objects that depend on it.  Delta chains are bounded by ``max_depth``.

Each pack record is self-describing so the index can be rebuilt from the
packs alone (``reindex``)::

    magic 'P' | otype:4bit kind:4bit | rawlen u32 | paylen u32 | [base id 32B] | payload
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import zstandard as zstd

BLOCK_SIZE = 8192

# object types
OT_BLOB = 0      # a data page / file chunk
OT_BLIST = 1     # list of block ids for a file (chunked)
OT_TREE = 2      # tree (manifest) of a snapshot
OT_COMMIT = 3    # commit metadata
OT_DICT = 4      # trained zstd dictionary

# encodings
K_RAW = 0
K_ZSTD = 1
K_XOR = 2
K_ZDICT = 3
K_TDICT = 4
BASED_KINDS = (K_XOR, K_ZDICT, K_TDICT)   # records that carry a 32-byte base id

KIND_NAMES = {K_RAW: "raw", K_ZSTD: "zstd", K_XOR: "xor", K_ZDICT: "zdict", K_TDICT: "tdict"}
OTYPE_NAMES = {OT_BLOB: "blob", OT_BLIST: "blocklist", OT_TREE: "tree", OT_COMMIT: "commit",
               OT_DICT: "dict"}

REC_HDR = struct.Struct("<cBII")  # magic, otype<<4|kind, rawlen, paylen
REC_MAGIC = b"P"
PACK_HEADER = b"PGITPACK1\n"
PACK_ROTATE = 1 << 30  # start a new pack after ~1 GiB

DEFAULT_CONFIG = {
    "format": 1,
    "block_size": BLOCK_SIZE,
    "zstd_level": 3,
    # delta: "auto" (try xor and zdict, keep smallest), "xor", "zdict", "none"
    "delta": "auto",
    "max_depth": 8,
    # keep a delta only if it is smaller than this fraction of the full encoding
    "delta_min_gain": 0.9,
    # trained dictionary for full pages: size in bytes (0 disables) and the number
    # of new pages buffered from the first backup to train it on
    "dict_size": 112640,
    "dict_samples": 4096,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id     BLOB PRIMARY KEY,
    otype  INTEGER NOT NULL,
    kind   INTEGER NOT NULL,
    pack   INTEGER NOT NULL,
    off    INTEGER NOT NULL,      -- offset of payload in pack
    len    INTEGER NOT NULL,      -- payload length
    rawlen INTEGER NOT NULL,
    base   BLOB,                  -- unused (base ids live in the pack record header)
    depth  INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS commits (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    id     BLOB UNIQUE NOT NULL,
    parent BLOB,
    time   TEXT NOT NULL,
    label  TEXT
);
CREATE TABLE IF NOT EXISTS refs (
    name   TEXT PRIMARY KEY,
    id     BLOB NOT NULL
);
"""


class ObjectMissing(KeyError):
    pass


class IntegrityError(Exception):
    pass


def oid(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def xor_bytes(a: bytes, b: bytes) -> bytes:
    n = len(a)
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(n, "little")


@dataclass(slots=True)
class ObjMeta:
    otype: int
    kind: int
    pack: int
    off: int
    len: int
    rawlen: int
    base: bytes | None
    depth: int


class Store:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        cfg_path = self.path / "config.json"
        if not cfg_path.exists():
            raise FileNotFoundError(f"{self.path} is not a pgit repository (run `pgit init`)")
        self.config = {**DEFAULT_CONFIG, **json.loads(cfg_path.read_text())}
        self.block_size = self.config["block_size"]
        self.db = sqlite3.connect(self.path / "index.sqlite", isolation_level=None,
                                  check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._local = threading.local()
        self._fds: dict[int, int] = {}
        self._fd_lock = threading.Lock()
        self._mem_index: dict[bytes, ObjMeta] | None = None
        self._cache: OrderedDict[bytes, bytes] = OrderedDict()
        self._cache_max = 4096
        self._cache_lock = threading.Lock()
        # writer state
        self._wpack: int | None = None
        self._wfile = None
        self._woff = 0
        self._pending: dict[bytes, ObjMeta] = {}
        # trained dictionary
        self._train_buf: dict[bytes, tuple[bytes, bytes | None]] = {}
        row = self.db.execute("SELECT id FROM objects WHERE otype=? ORDER BY pack DESC, off DESC "
                              "LIMIT 1", (OT_DICT,)).fetchone()
        self._dict_id: bytes | None = row[0] if row else None
        self._dict_cctx = None
        self.stats_reset()

    # ------------------------------------------------------------------ setup
    @classmethod
    def init(cls, path: str | os.PathLike, **overrides) -> "Store":
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        if (p / "config.json").exists():
            raise FileExistsError(f"{p} already contains a pgit repository")
        (p / "packs").mkdir(exist_ok=True)
        cfg = {**DEFAULT_CONFIG, **{k: v for k, v in overrides.items() if v is not None}}
        if cfg["delta"] not in ("auto", "xor", "zdict", "none"):
            raise ValueError("delta must be one of auto, xor, zdict, none")
        (p / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        return cls(p)

    def close(self):
        self.finish_pack()
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------- compressors
    def _cctx(self) -> zstd.ZstdCompressor:
        c = getattr(self._local, "cctx", None)
        if c is None:
            c = zstd.ZstdCompressor(level=self.config["zstd_level"], write_checksum=False,
                                    write_content_size=False, write_dict_id=False)
            self._local.cctx = c
        return c

    def _dict_compressor(self):
        if self._dict_id is None:
            return None
        if self._dict_cctx is None:
            d = zstd.ZstdCompressionDict(self.get(self._dict_id))
            self._dict_cctx = zstd.ZstdCompressor(level=self.config["zstd_level"], dict_data=d,
                                                  write_checksum=False, write_content_size=False,
                                                  write_dict_id=False)
        return self._dict_cctx

    def _dict_dctx(self, dict_id: bytes) -> zstd.ZstdDecompressor:
        cache = getattr(self._local, "ddicts", None)
        if cache is None:
            cache = self._local.ddicts = {}
        d = cache.get(dict_id)
        if d is None:
            d = zstd.ZstdDecompressor(dict_data=zstd.ZstdCompressionDict(self.get(dict_id)))
            cache[dict_id] = d
        return d

    def _dctx(self) -> zstd.ZstdDecompressor:
        d = getattr(self._local, "dctx", None)
        if d is None:
            d = zstd.ZstdDecompressor()
            self._local.dctx = d
        return d

    # ---------------------------------------------------------------- stats
    def stats_reset(self):
        self.stats = {"new_objects": 0, "new_raw_bytes": 0, "new_stored_bytes": 0,
                      "kinds": {name: 0 for name in KIND_NAMES.values()},
                      "kind_bytes": {name: 0 for name in KIND_NAMES.values()}}

    # ---------------------------------------------------------------- index
    def preload(self):
        """Load the whole object index into memory (fast, thread-safe reads)."""
        idx = {}
        for row in self.db.execute(
                "SELECT id, otype, kind, pack, off, len, rawlen, base, depth FROM objects"):
            idx[row[0]] = ObjMeta(*row[1:])
        self._mem_index = idx

    def meta(self, id_: bytes) -> ObjMeta | None:
        m = self._pending.get(id_)
        if m is not None:
            return m
        if self._mem_index is not None:
            return self._mem_index.get(id_)
        row = self.db.execute(
            "SELECT otype, kind, pack, off, len, rawlen, base, depth FROM objects WHERE id=?",
            (id_,)).fetchone()
        return ObjMeta(*row) if row else None

    def has(self, id_: bytes) -> bool:
        if id_ in self._pending or id_ in self._train_buf:
            return True
        if self._mem_index is not None:
            return id_ in self._mem_index
        return self.db.execute("SELECT 1 FROM objects WHERE id=?", (id_,)).fetchone() is not None

    # ---------------------------------------------------------------- packs
    def _pack_path(self, n: int) -> Path:
        return self.path / "packs" / f"pack-{n:06d}.pack"

    def pack_numbers(self) -> list[int]:
        out = []
        for f in (self.path / "packs").glob("pack-*.pack"):
            out.append(int(f.stem.split("-")[1]))
        return sorted(out)

    def _open_writer(self):
        nums = self.pack_numbers()
        n = (nums[-1] + 1) if nums else 1
        self._wpack = n
        self._wfile = open(self._pack_path(n), "wb")
        self._wfile.write(PACK_HEADER)
        self._woff = len(PACK_HEADER)

    def finish_pack(self, sync: bool = True):
        """Flush the current pack to disk and commit pending index entries."""
        self._drain_train_buf()
        if self._wfile is not None:
            self._wfile.flush()
            if sync:
                os.fsync(self._wfile.fileno())
            self._wfile.close()
            self._wfile = None
            self._wpack = None
        if self._pending:
            self._flush_index(sync=False)

    def _append(self, otype: int, kind: int, rawlen: int, payload: bytes,
                base: bytes | None) -> tuple[int, int]:
        if self._wfile is None or self._woff > PACK_ROTATE:
            if self._wfile is not None:
                self.finish_pack()
            self._open_writer()
        hdr = REC_HDR.pack(REC_MAGIC, (otype << 4) | kind, rawlen, len(payload))
        self._wfile.write(hdr)
        off = self._woff + REC_HDR.size
        if kind in BASED_KINDS:
            self._wfile.write(base)
            off += 32
        self._wfile.write(payload)
        self._woff = off + len(payload)
        return self._wpack, off

    def _fd(self, pack: int) -> int:
        fd = self._fds.get(pack)
        if fd is None:
            with self._fd_lock:
                fd = self._fds.get(pack)
                if fd is None:
                    if self._wfile is not None and pack == self._wpack:
                        self._wfile.flush()
                    fd = os.open(self._pack_path(pack), os.O_RDONLY)
                    self._fds[pack] = fd
        return fd

    # ---------------------------------------------------------------- write
    def _encode(self, data: bytes, base: bytes | None, base_raw: bytes | None,
                mode: str, otype: int = OT_BLOB) -> tuple[int, bytes, bytes | None]:
        """Pick the smallest encoding. Returns (kind, payload, base_or_None)."""
        cctx = self._cctx()
        full = cctx.compress(data)
        if len(full) >= len(data):
            best = (K_RAW, data, None)
        else:
            best = (K_ZSTD, full, None)
        dc = self._dict_compressor() if otype == OT_BLOB else None
        if dc is not None:
            fd = dc.compress(data)
            if len(fd) < len(best[1]):
                best = (K_TDICT, fd, self._dict_id)
        if base_raw is None or mode == "none":
            return best
        limit = len(best[1]) * self.config["delta_min_gain"]
        cands = []
        if mode in ("auto", "xor") and len(base_raw) == len(data):
            cands.append((K_XOR, cctx.compress(xor_bytes(data, base_raw))))
        if mode in ("auto", "zdict"):
            d = zstd.ZstdCompressionDict(base_raw, dict_type=zstd.DICT_TYPE_RAWCONTENT)
            c = zstd.ZstdCompressor(level=self.config["zstd_level"], dict_data=d,
                                    write_checksum=False, write_content_size=False,
                                    write_dict_id=False)
            cands.append((K_ZDICT, c.compress(data)))
        for kind, payload in cands:
            if len(payload) < limit:
                best = (kind, payload, base)
                limit = len(payload)
        return best

    def put(self, data: bytes, otype: int = OT_BLOB, base: bytes | None = None,
            id_: bytes | None = None) -> bytes:
        """Store ``data`` (if not already present) and return its id.

        ``base`` is a hint: the id of an existing object that is probably similar
        (e.g. the same page in the previous backup).  It is used as a delta base
        if that saves space and the resulting chain stays within ``max_depth``.
        """
        if id_ is None:
            id_ = oid(data)
        if self.has(id_):
            return id_
        if (otype == OT_BLOB and self._dict_id is None and self.config["dict_size"]
                and len(self._train_buf) < self.config["dict_samples"]):
            # no dictionary yet: hold the first pages back to train one on them
            self._train_buf[id_] = (data, base)
            if len(self._train_buf) >= self.config["dict_samples"]:
                self._drain_train_buf()
            return id_
        return self._put_now(data, otype, base, id_)

    def flush_buffers(self):
        self._drain_train_buf()

    def _drain_train_buf(self):
        """Train the dictionary on buffered pages (if enough), then store them."""
        if not self._train_buf:
            return
        buf = self._train_buf
        self._train_buf = {}
        if self._dict_id is None and len(buf) >= 256:
            try:
                d = zstd.train_dictionary(self.config["dict_size"], [v[0] for v in buf.values()],
                                          level=self.config["zstd_level"])
                self._dict_id = self._put_now(d.as_bytes(), OT_DICT, None, None)
                self._dict_cctx = None
            except zstd.ZstdError:
                pass
        for id_, (data, base) in buf.items():
            self._put_now(data, OT_BLOB, base, id_)

    def _put_now(self, data: bytes, otype: int, base: bytes | None,
                 id_: bytes | None) -> bytes:
        if id_ is None:
            id_ = oid(data)
        if id_ in self._pending or (self._mem_index is not None and id_ in self._mem_index):
            return id_
        base_raw = None
        depth = 0
        if base is not None and base != id_ and self.config["delta"] != "none":
            bm = self.meta(base)
            if bm is not None and bm.depth < self.config["max_depth"]:
                base_raw = self.get(base)
                depth = bm.depth + 1
        kind, payload, used_base = self._encode(data, base, base_raw, self.config["delta"], otype)
        if used_base is None or kind == K_TDICT:
            depth = 0
        pack, off = self._append(otype, kind, len(data), payload, used_base)
        self._pending[id_] = ObjMeta(otype, kind, pack, off, len(payload), len(data),
                                     used_base, depth)
        st = self.stats
        st["new_objects"] += 1
        st["new_raw_bytes"] += len(data)
        st["new_stored_bytes"] += len(payload) + REC_HDR.size + (32 if used_base else 0)
        st["kinds"][KIND_NAMES[kind]] += 1
        st["kind_bytes"][KIND_NAMES[kind]] += len(payload)
        if len(self._pending) >= 50000:
            self._flush_index()
        return id_

    def _flush_index(self, sync: bool = True):
        """Persist pending index rows.

        The pack is flushed and fsync'ed first so that an index row never points
        at data that could be lost in a crash (later backups dedup against it).
        """
        if self._wfile is not None:
            self._wfile.flush()
            if sync:
                os.fsync(self._wfile.fileno())
        self.db.execute("BEGIN")
        self.db.executemany(
            "INSERT OR IGNORE INTO objects (id, otype, kind, pack, off, len, rawlen, base, depth)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(k, m.otype, m.kind, m.pack, m.off, m.len, m.rawlen, None, m.depth)
             for k, m in self._pending.items()])
        self.db.execute("COMMIT")
        if self._mem_index is not None:
            self._mem_index.update(self._pending)
        self._pending.clear()

    # ----------------------------------------------------------------- read
    def base_of(self, m: ObjMeta) -> bytes | None:
        """Delta base / dictionary id of an object.

        It is not kept in the index (it is already in the pack record header,
        and would cost 32 bytes per row); read it from the pack when needed.
        """
        if m.kind not in BASED_KINDS:
            return None
        if m.base is not None:
            return m.base
        b = os.pread(self._fd(m.pack), 32, m.off - 32)
        if len(b) != 32:
            raise IntegrityError(f"short read in pack {m.pack} at {m.off - 32}")
        return b

    def read_payload(self, m: ObjMeta) -> bytes:
        buf = os.pread(self._fd(m.pack), m.len, m.off)
        if len(buf) != m.len:
            raise IntegrityError(f"short read in pack {m.pack} at {m.off}")
        return buf

    def get(self, id_: bytes, verify: bool = True, _depth: int = 0) -> bytes:
        buffered = self._train_buf.get(id_)
        if buffered is not None:
            return buffered[0]
        with self._cache_lock:
            hit = self._cache.get(id_)
            if hit is not None:
                self._cache.move_to_end(id_)
                return hit
        m = self.meta(id_)
        if m is None:
            raise ObjectMissing(id_.hex())
        if _depth > 64:
            raise IntegrityError("delta chain too deep / cyclic")
        if m.kind in BASED_KINDS and m.base is None:
            buf = os.pread(self._fd(m.pack), m.len + 32, m.off - 32)
            if len(buf) != m.len + 32:
                raise IntegrityError(f"short read in pack {m.pack} at {m.off}")
            mbase, payload = buf[:32], buf[32:]
        else:
            mbase, payload = m.base, self.read_payload(m)
        try:
            if m.kind == K_RAW:
                data = payload
            elif m.kind == K_ZSTD:
                data = self._dctx().decompress(payload, max_output_size=m.rawlen)
            elif m.kind == K_XOR:
                base = self.get(mbase, verify, _depth + 1)
                data = xor_bytes(self._dctx().decompress(payload, max_output_size=m.rawlen), base)
            elif m.kind == K_ZDICT:
                base = self.get(mbase, verify, _depth + 1)
                d = zstd.ZstdCompressionDict(base, dict_type=zstd.DICT_TYPE_RAWCONTENT)
                data = zstd.ZstdDecompressor(dict_data=d).decompress(payload, max_output_size=m.rawlen)
            elif m.kind == K_TDICT:
                data = self._dict_dctx(mbase).decompress(payload, max_output_size=m.rawlen)
            else:
                raise IntegrityError(f"unknown encoding {m.kind}")
        except zstd.ZstdError as e:
            raise IntegrityError(f"object {id_.hex()} failed to decode: {e}") from e
        if len(data) != m.rawlen or (verify and oid(data) != id_):
            raise IntegrityError(f"object {id_.hex()} hash mismatch (corrupt pack {m.pack}"
                                 f" @ {m.off})")
        # cache small objects that are likely to be re-used (bases, zero pages)
        if m.depth < self.config["max_depth"] or m.otype != OT_BLOB:
            with self._cache_lock:
                self._cache[id_] = data
                if len(self._cache) > self._cache_max:
                    self._cache.popitem(last=False)
        return data

    # ------------------------------------------------------------ json objs
    def put_json(self, obj, otype: int, base: bytes | None = None) -> bytes:
        data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
        return self.put(data, otype, base=base)

    def get_json(self, id_: bytes):
        return json.loads(self.get(id_))

    # -------------------------------------------------------------- commits
    def add_commit(self, commit: dict) -> bytes:
        cid = self.put_json(commit, OT_COMMIT)
        self.finish_pack()
        self.db.execute("INSERT OR IGNORE INTO commits (id, parent, time, label) VALUES (?,?,?,?)",
                        (cid, bytes.fromhex(commit["parent"]) if commit.get("parent") else None,
                         commit["time"], commit.get("label")))
        return cid

    def commits(self) -> list[tuple[int, bytes, bytes | None, str, str | None]]:
        return list(self.db.execute("SELECT seq, id, parent, time, label FROM commits ORDER BY seq"))

    def head(self) -> bytes | None:
        row = self.db.execute("SELECT id FROM commits ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def set_ref(self, name: str, cid: bytes):
        self.db.execute("INSERT OR REPLACE INTO refs (name, id) VALUES (?,?)", (name, cid))

    def del_ref(self, name: str):
        self.db.execute("DELETE FROM refs WHERE name=?", (name,))

    def refs(self) -> dict[str, bytes]:
        return dict(self.db.execute("SELECT name, id FROM refs"))

    def resolve(self, ref: str | None) -> bytes:
        """Resolve HEAD, HEAD~N, @SEQ, a tag name, a label or a hex prefix to a commit id."""
        commits = self.commits()
        if not commits:
            raise KeyError("repository has no commits")
        if ref is None or ref == "HEAD":
            return commits[-1][1]
        if ref.startswith("HEAD~"):
            n = int(ref[5:] or 1)
            if n >= len(commits):
                raise KeyError(f"{ref}: only {len(commits)} commits")
            return commits[-1 - n][1]
        if ref.startswith("@"):
            seq = int(ref[1:])
            for c in commits:
                if c[0] == seq:
                    return c[1]
            raise KeyError(f"no commit with sequence {seq}")
        refs = self.refs()
        if ref in refs:
            return refs[ref]
        by_label = [c[1] for c in commits if c[4] == ref]
        if len(by_label) == 1:
            return by_label[0]
        ref_l = ref.lower()
        matches = [c[1] for c in commits if c[1].hex().startswith(ref_l)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KeyError(f"{ref}: ambiguous commit prefix")
        raise KeyError(f"{ref}: unknown revision")

    def seq_of(self, cid: bytes) -> int | None:
        row = self.db.execute("SELECT seq FROM commits WHERE id=?", (cid,)).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- sizes
    def disk_usage(self) -> dict:
        packs = sum(self._pack_path(n).stat().st_size for n in self.pack_numbers())
        idx = 0
        for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm"):
            f = self.path / name
            if f.exists():
                idx += f.stat().st_size
        return {"packs": packs, "index": idx, "total": packs + idx}

    # -------------------------------------------------------------- reindex
    def scan_pack(self, n: int):
        """Yield (otype, kind, rawlen, payload_off, paylen, base) for every record."""
        with open(self._pack_path(n), "rb") as f:
            if f.read(len(PACK_HEADER)) != PACK_HEADER:
                raise IntegrityError(f"pack {n}: bad header")
            off = len(PACK_HEADER)
            while True:
                hdr = f.read(REC_HDR.size)
                if not hdr:
                    return
                if len(hdr) < REC_HDR.size:
                    raise IntegrityError(f"pack {n}: truncated record at {off}")
                magic, tk, rawlen, paylen = REC_HDR.unpack(hdr)
                if magic != REC_MAGIC:
                    raise IntegrityError(f"pack {n}: bad record magic at {off}")
                otype, kind = tk >> 4, tk & 0xF
                off += REC_HDR.size
                base = None
                if kind in BASED_KINDS:
                    base = f.read(32)
                    off += 32
                yield otype, kind, rawlen, off, paylen, base
                f.seek(paylen, 1)
                off += paylen
