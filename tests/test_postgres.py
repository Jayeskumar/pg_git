"""End-to-end test against a real PostgreSQL server (skipped when unavailable).

PostgreSQL refuses to run as root, so run it as an ordinary user, e.g.
``su postgres -c "python3 -m pytest tests/test_postgres.py"``.
"""
import os
import shutil
import subprocess

import pytest

from pgit import restore as rmod
from pgit.pgutil import basebackup_stream, catalog_maps, pg_bin
from pgit.snapshot import entries_from_tar, snapshot
from pgit.store import Store

try:
    BIN = os.path.dirname(pg_bin("postgres"))
except FileNotFoundError:
    BIN = None

pytestmark = pytest.mark.skipif(BIN is None or os.geteuid() == 0,
                                reason="needs PostgreSQL server binaries and a non-root user")


def sh(*cmd, **kw):
    env = dict(os.environ, PATH=f"{BIN}:{os.environ['PATH']}")
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env, **kw).stdout


class PG:
    def __init__(self, datadir, port, sock):
        self.d, self.port, self.sock = str(datadir), port, str(sock)
        self.conn = f"host={sock} port={port} dbname=postgres"

    def start(self):
        sh("pg_ctl", "-D", self.d, "-w", "-l", self.d + ".log", "-o",
           f"-p {self.port} -k {self.sock} -c listen_addresses='' -c archive_mode=off", "start")

    def stop(self):
        sh("pg_ctl", "-D", self.d, "-m", "fast", "-w", "stop")

    def q(self, sql):
        return sh("psql", "-X", "-A", "-t", "-d", self.conn, "-c", sql).strip()


@pytest.fixture
def cluster(tmp_path):
    sock = tmp_path / "s"
    sock.mkdir()
    pg = PG(tmp_path / "data", 55432, sock)
    sh("initdb", "-D", pg.d, "-A", "trust", "--no-sync", "--data-checksums")
    with open(os.path.join(pg.d, "postgresql.conf"), "a") as f:
        f.write("wal_level=replica\nmax_wal_senders=4\nwal_keep_size='256MB'\n")
    pg.start()
    yield pg
    pg.stop()


STATE = "SELECT count(*) || ':' || sum(abalance) || ':' || (SELECT count(*) FROM t) FROM pgbench_accounts"


def test_backup_versions_restore_and_recover(cluster, tmp_path):
    pg = cluster
    sh("pgbench", "-i", "-q", "-s", "2", "-d", pg.conn)
    pg.q("CREATE TABLE t AS SELECT g AS id, md5(g::text) AS v FROM generate_series(1, 5000) g")
    store = Store.init(tmp_path / "repo")
    expected = []
    for v in range(3):
        if v:
            sh("pgbench", "-n", "-t", "300", "-c", "2", "-d", pg.conn)
            pg.q(f"INSERT INTO t SELECT g, 'v{v}' FROM generate_series(1, 1000) g")
        pg.q("CHECKPOINT")
        expected.append(pg.q(STATE))
        relmap, dbmap = catalog_maps(pg.conn)
        with basebackup_stream(pg.conn, label=f"v{v}") as s:
            snapshot(store, entries_from_tar(s), store.head(), f"v{v}",
                     {"relmap": relmap, "dbmap": dbmap})
    commits = [c[1] for c in store.commits()]
    d = rmod.diff(store, commits[0], commits[-1])
    assert any("pgbench_accounts" in k for k in d["groups"])
    for v, cid in enumerate(commits):
        target = tmp_path / f"r{v}"
        rmod.restore(store, cid, str(target), jobs=2)
        sh("pg_verifybackup", "-n", str(target))
        r = PG(target, 55433, pg.sock)
        r.start()
        try:
            assert r.q(STATE) == expected[v]
        finally:
            r.stop()
        shutil.rmtree(target)
    assert rmod.fsck(store)["errors"] == []
    store.close()
