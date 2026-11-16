"""Unit tests that need no PostgreSQL server: synthetic data directories."""
import hashlib
import os
import random

import pytest

from pgit import gc as gcmod
from pgit import restore as rmod
from pgit.snapshot import entries_from_dir, snapshot
from pgit.store import BLOCK_SIZE, IntegrityError, Store

BS = BLOCK_SIZE


def make_page(rng, n):
    """A fake heap page: header + repetitive tuples + free space (compressible)."""
    hdr = n.to_bytes(8, "little") + bytes(16)
    body = b"".join(f"tuple-{n}-{k:05d}-{'x' * 40}|".encode() for k in range(60))
    page = hdr + bytes(BS - len(hdr) - len(body)) + body
    return bytearray(page[:BS])


def build_datadir(root, rng, nrel=3, pages=64):
    os.makedirs(os.path.join(root, "base", "5"), exist_ok=True)
    os.makedirs(os.path.join(root, "global"), exist_ok=True)
    os.makedirs(os.path.join(root, "pg_notify"), exist_ok=True)  # empty dir must survive
    for r in range(nrel):
        with open(os.path.join(root, "base", "5", str(16384 + r)), "wb") as f:
            for p in range(pages):
                f.write(make_page(rng, r * 1000 + p))
    with open(os.path.join(root, "PG_VERSION"), "w") as f:
        f.write("16\n")
    with open(os.path.join(root, "global", "pg_control"), "wb") as f:
        f.write(os.urandom(8192))
    with open(os.path.join(root, "odd_size_file"), "wb") as f:
        f.write(os.urandom(12345))
    os.symlink("PG_VERSION", os.path.join(root, "a_symlink"))


def mutate(root, rng, frac=0.1):
    for r in range(3):
        path = os.path.join(root, "base", "5", str(16384 + r))
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            for p in range(size // BS):
                if rng.random() < frac:
                    f.seek(p * BS + 8)
                    f.write(rng.randbytes(4))          # "LSN" in header
                    f.seek(p * BS + 2000)
                    f.write(rng.randbytes(64))         # a changed tuple
            f.seek(0, 2)
            f.write(bytes(make_page(rng, rng.randrange(1 << 30))))   # extension
    with open(os.path.join(root, "global", "pg_control"), "r+b") as f:
        f.write(os.urandom(16))


def tree_digest(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            full = os.path.join(dirpath, d)
            out[os.path.relpath(full, root)] = "link:" + os.readlink(full) if os.path.islink(full) else "dir"
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                out[rel] = "link:" + os.readlink(full)
            else:
                out[rel] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    return out


@pytest.fixture
def env(tmp_path):
    rng = random.Random(42)
    src = tmp_path / "src"
    build_datadir(str(src), rng)
    store = Store.init(tmp_path / "repo", max_depth=3)
    yield store, src, rng, tmp_path
    store.close()


def take(store, src):
    cid, commit = snapshot(store, entries_from_dir(str(src)), store.head())
    return cid, commit


def test_roundtrip_many_versions(env):
    store, src, rng, tmp = env
    versions = []
    for v in range(6):
        if v:
            mutate(str(src), rng)
        cid, commit = take(store, src)
        versions.append((cid, tree_digest(str(src)), commit))
    # deltas must be much smaller than the first full snapshot
    first = versions[0][2]["stats"]["store"]["new_stored_bytes"]
    for _cid, _d, c in versions[1:]:
        assert c["stats"]["store"]["new_stored_bytes"] < first / 3
        assert c["stats"]["store"]["kinds"]["xor"] + c["stats"]["store"]["kinds"]["zdict"] > 0
    # delta chains are bounded
    maxd = store.db.execute("SELECT max(depth) FROM objects").fetchone()[0]
    assert maxd <= 3
    for i, (cid, digest, _c) in enumerate(versions):
        out = tmp / f"restore{i}"
        rmod.restore(store, cid, str(out), jobs=3)
        assert tree_digest(str(out)) == digest
        assert (out / "pg_notify").is_dir()
        assert rmod.verify_commit(store, cid)["errors"] == []


def test_delta_restore_switches_versions(env):
    store, src, rng, tmp = env
    c1, _ = take(store, src)
    d1 = tree_digest(str(src))
    mutate(str(src), rng)
    os.unlink(src / "odd_size_file")
    c2, _ = take(store, src)
    d2 = tree_digest(str(src))
    out = tmp / "co"
    rmod.restore(store, c2, str(out))
    (out / "junk").write_text("not part of the backup")
    st = rmod.restore(store, c1, str(out), delta=True)
    assert tree_digest(str(out)) == d1
    assert st["skipped_blocks"] > st["written_blocks"]
    rmod.restore(store, c2, str(out), delta=True)
    assert tree_digest(str(out)) == d2


def test_dedup_identical_snapshot_costs_nothing(env):
    store, src, _rng, _tmp = env
    take(store, src)
    _cid, c = take(store, src)
    assert c["stats"]["new_blocks"] == 0
    assert c["stats"]["store"]["new_stored_bytes"] < 2048   # just tree + commit


def test_corruption_detected(env):
    store, src, rng, tmp = env
    c1, _ = take(store, src)
    mutate(str(src), rng)
    c2, _ = take(store, src)
    store.close()
    pack = sorted((tmp / "repo" / "packs").glob("*.pack"))[0]
    data = bytearray(pack.read_bytes())
    data[len(data) // 3] ^= 0xFF
    pack.write_bytes(bytes(data))
    s2 = Store(tmp / "repo")
    try:
        r = rmod.fsck(s2)
        assert r["errors"]
        with pytest.raises((IntegrityError, Exception)):
            for cid in (c1, c2):
                v = rmod.verify_commit(s2, cid)
                if v["errors"]:
                    raise IntegrityError(v["errors"][0])
    finally:
        s2.close()


def test_rm_gc_keeps_remaining_versions_intact(env):
    store, src, rng, tmp = env
    cids = []
    digests = []
    for v in range(5):
        if v:
            mutate(str(src), rng, frac=0.3)
        cid, _ = take(store, src)
        cids.append(cid)
        digests.append(tree_digest(str(src)))
    before = store.disk_usage()["packs"]
    # dropping a middle snapshot re-deltas its dependants against older pages;
    # dropping the newest one frees its objects outright
    gcmod.remove_commit(store, cids[2])
    gcmod.remove_commit(store, cids[4])
    r = gcmod.gc(store)
    assert r["objects_dropped"] > 0
    assert store.disk_usage()["packs"] < before
    assert rmod.fsck(store)["errors"] == []
    # dropping the full base snapshot must still leave everything restorable
    gcmod.remove_commit(store, cids[0])
    gcmod.gc(store)
    assert rmod.fsck(store)["errors"] == []
    for i in (1, 3):
        out = tmp / f"after_gc{i}"
        rmod.restore(store, cids[i], str(out))
        assert tree_digest(str(out)) == digests[i]


def test_reindex_from_packs(env):
    store, src, rng, tmp = env
    c1, _ = take(store, src)
    mutate(str(src), rng)
    c2, _ = take(store, src)
    d2 = tree_digest(str(src))
    store.close()
    for f in (tmp / "repo").glob("index.sqlite*"):
        f.unlink()
    s2 = Store(tmp / "repo")
    try:
        r = gcmod.reindex(s2)
        assert r["commits_found"] == 2
        assert s2.resolve("HEAD") == c2
        assert s2.resolve("HEAD~1") == c1
        rmod.restore(s2, c2, str(tmp / "ri"))
        assert tree_digest(str(tmp / "ri")) == d2
    finally:
        s2.close()


def test_resolve_refs(env):
    store, src, rng, _tmp = env
    c1, _ = take(store, src)
    mutate(str(src), rng)
    c2, _ = take(store, src)
    store.set_ref("before-migration", c1)
    assert store.resolve("HEAD") == c2
    assert store.resolve("HEAD~1") == c1
    assert store.resolve("@1") == c1
    assert store.resolve("before-migration") == c1
    assert store.resolve(c2.hex()[:10]) == c2
