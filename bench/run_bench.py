#!/usr/bin/env python3
"""Benchmark pgit against existing PostgreSQL backup tools.

Run as the ``postgres`` OS user (PostgreSQL refuses to run as root)::

    python3 bench/run_bench.py --work /path/to/scratch --scale 30 --versions 10

For every version of a changing pgbench database it takes a backup with each
tool, records wall time and repository size, then restores selected versions,
starts PostgreSQL on them and compares a logical checksum of every table with
the value recorded at backup time.  Finally it injects a one-byte corruption
into each tool's repository and checks whether the tool notices.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

from pgit.pgutil import pg_bin  # noqa: E402

PGBIN = os.path.dirname(pg_bin("postgres"))
ENV = dict(os.environ)
ENV["PATH"] = PGBIN + ":" + ENV.get("PATH", "")
ENV["PYTHONPATH"] = str(REPO_ROOT)
PGIT = [sys.executable, "-m", "pgit"]

LOG = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


def run(cmd, timeout=3600, check=True, env=None, input=None, shell=False, cwd=None):
    t0 = time.time()
    p = subprocess.run(cmd, env=env or ENV, timeout=timeout, capture_output=True,
                       input=input, shell=shell, cwd=cwd)
    dt = time.time() - t0
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {cmd}\n"
                           f"{p.stdout.decode(errors='replace')[-2000:]}\n"
                           f"{p.stderr.decode(errors='replace')[-2000:]}")
    return dt, p


def du(path) -> int:
    """Apparent size in bytes of all regular files below ``path``."""
    total = 0
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total


def rmrf(p):
    if os.path.lexists(p):
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


# ----------------------------------------------------------------- cluster
class Cluster:
    def __init__(self, datadir, port, sockdir):
        self.datadir = str(datadir)
        self.port = port
        self.sockdir = str(sockdir)

    @property
    def conn(self):
        return f"host={self.sockdir} port={self.port} dbname=bench"

    def start(self, extra="", timeout=900):
        opts = f"-p {self.port} -k {self.sockdir} -c listen_addresses='' {extra}"
        dt, _ = run(["pg_ctl", "-D", self.datadir, "-o", opts, "-l", self.datadir + ".log",
                     "-w", "-t", str(timeout), "start"], timeout=timeout + 60)
        return dt

    def stop(self, check=True):
        run(["pg_ctl", "-D", self.datadir, "-m", "fast", "-w", "stop"], check=check)

    def psql(self, sql, db="bench"):
        _, p = run(["psql", "-X", "-q", "-A", "-t", "-h", self.sockdir, "-p", str(self.port),
                    "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql])
        return p.stdout.decode().strip()

    def checksums(self) -> dict:
        tables = self.psql("SELECT string_agg(relname, ' ' ORDER BY relname) FROM pg_class "
                           "WHERE relkind='r' AND relnamespace='public'::regnamespace").split()
        out = {}
        for t in tables:
            out[t] = self.psql(
                f"SELECT count(*) || ':' || coalesce(sum(('x'||substr(md5(t::text),1,15))"
                f"::bit(60)::bigint),0) FROM public.{t} t")
        return out


# ---------------------------------------------------------------- workload
def workload_steps(c: Cluster, scale: int, txns: int, clients: int, rng: random.Random):
    pgb = ["pgbench", "-h", c.sockdir, "-p", str(c.port), "-n", "-c", str(clients),
           "-j", str(clients), "-t", str(txns), "bench"]

    def oltp():
        run(pgb, timeout=7200)
        return f"pgbench {clients}x{txns} tx"

    def bulk_insert():
        n = 40000 * max(1, scale // 10)
        c.psql(f"INSERT INTO events (kind, payload) SELECT (g % 7), "
               f"jsonb_build_object('n', g, 'r', md5(g::text), 'tags', array[g%3, g%5]) "
               f"FROM generate_series(1, {n}) g")
        return f"bulk insert {n} events"

    def update_slice():
        lo = rng.randrange(1, scale * 100000 - 100000)
        c.psql(f"UPDATE pgbench_accounts SET filler = md5(aid::text) "
               f"WHERE aid BETWEEN {lo} AND {lo + 5000 * scale}")
        return f"update {5000 * scale} contiguous accounts"

    def create_index():
        c.psql("CREATE INDEX IF NOT EXISTS events_kind_idx ON events (kind)")
        return "create index on events"

    def delete_vacuum():
        c.psql("DELETE FROM events WHERE id % 5 = 0")
        c.psql("VACUUM events")
        return "delete 20% events + vacuum"

    def rewrite():
        c.psql("VACUUM FULL events")
        return "VACUUM FULL events (table rewrite)"

    def ddl_small():
        c.psql("CREATE TABLE IF NOT EXISTS audit (id bigserial primary key, at timestamptz "
               "default now(), note text); INSERT INTO audit (note) SELECT 'n'||g "
               "FROM generate_series(1,1000) g")
        return "create small audit table"

    plan = [oltp, bulk_insert, oltp, update_slice, create_index, oltp, delete_vacuum,
            ddl_small, oltp, rewrite, oltp, bulk_insert, oltp]
    return plan


# ------------------------------------------------------------------ tools
class Tool:
    name = "tool"
    restorable = True

    def __init__(self, work: Path):
        self.work = work

    def setup(self, cluster):
        pass

    def backup(self, v: int, cluster) -> float:
        raise NotImplementedError

    def size(self) -> int:
        raise NotImplementedError

    def size_detail(self) -> dict:
        return {}

    def restore(self, v: int, target: str) -> float:
        raise NotImplementedError

    def corrupt_and_check(self) -> dict:
        return {"detected": None, "note": "not tested"}


def flip_byte(path: Path, frac=0.5) -> int:
    os.chmod(path, 0o600)   # restic/git keep their objects read-only
    with open(path, "r+b") as f:
        f.seek(0, 2)
        size = f.tell()
        off = int(size * frac)
        f.seek(off)
        b = f.read(1)
        f.seek(off)
        f.write(bytes([b[0] ^ 0x5A]))
    return off


def largest_file(root: Path, pattern="*") -> Path:
    files = [p for p in root.rglob(pattern) if p.is_file() and not p.is_symlink()]
    return max(files, key=lambda p: p.stat().st_size)


class PgitTool(Tool):
    name = "pgit"

    def __init__(self, work, delta="auto", max_depth=8, name=None, from_tar_of=None):
        super().__init__(work)
        self.delta = delta
        self.max_depth = max_depth
        if name:
            self.name = name
        self.repo = work / self.name
        self.from_tar_of = from_tar_of     # a BaseBackupTool whose tar we ingest
        self.restorable = from_tar_of is None
        self.stats = []

    def setup(self, cluster):
        rmrf(self.repo)
        run(PGIT + ["-C", str(self.repo), "init", "--delta", self.delta,
                    "--max-depth", str(self.max_depth)])

    def backup(self, v, cluster):
        if self.from_tar_of is not None:
            tar = self.from_tar_of.dir(v) / "base.tar.zst"
            dt, p = run(f"zstd -qdc {tar} | {' '.join(PGIT)} -C {self.repo} backup "
                        f"--from-tar - -l v{v} --json", shell=True)
        else:
            dt, p = run(PGIT + ["-C", str(self.repo), "backup", "-d", cluster.conn,
                                "-l", f"v{v}", "--json"])
        self.stats.append(json.loads(p.stdout.decode().strip().splitlines()[-1]))
        return dt

    def size(self):
        return du(self.repo)

    def size_detail(self):
        return {"packs": du(self.repo / "packs"),
                "index": sum(du(f) for f in self.repo.glob("index.sqlite*"))}

    def restore(self, v, target):
        dt, _ = run(PGIT + ["-C", str(self.repo), "restore", "-j", "4", f"v{v}", target])
        return dt

    def corrupt_and_check(self):
        copy = self.work / (self.name + "-corrupt")
        rmrf(copy)
        shutil.copytree(self.repo, copy)
        pack = sorted((copy / "packs").glob("*.pack"))[0]
        off = flip_byte(pack)
        _, p = run(PGIT + ["-C", str(copy), "fsck"], check=False)
        out = p.stdout.decode()
        _, pv = run(PGIT + ["-C", str(copy), "verify", "--all"], check=False)
        affected = sum(1 for line in pv.stdout.decode().splitlines() if "ERRORS" in line)
        total = sum(1 for line in pv.stdout.decode().splitlines() if "files" in line)
        rmrf(copy)
        return {"detected": p.returncode != 0, "how": "pgit fsck", "file": pack.name, "offset": off,
                "tool_output": out.strip().splitlines()[-1],
                "versions_affected": f"{affected}/{total}"}


class BaseBackupTool(Tool):
    """A full pg_basebackup (tar, zstd) for every version."""
    name = "pg_basebackup (full, zstd)"

    def __init__(self, work):
        super().__init__(work)
        self.root = work / "basebackup"

    def dir(self, v):
        return self.root / f"v{v}"

    def setup(self, cluster):
        rmrf(self.root)
        self.root.mkdir()

    def backup(self, v, cluster):
        dt, _ = run(["pg_basebackup", "-d", cluster.conn, "-D", str(self.dir(v)), "-F", "tar",
                     "-X", "fetch", "-Z", "client-zstd:level=3", "-c", "fast", "-l", f"v{v}"])
        return dt

    def size(self):
        return du(self.root)

    def restore(self, v, target):
        os.makedirs(target, mode=0o700)
        dt, _ = run(["tar", "-I", "zstd", "-xf", str(self.dir(v) / "base.tar.zst"), "-C", target])
        return dt

    def verify_restored(self, v, target):
        _, p = run(["pg_verifybackup", "-n", "-m", str(self.dir(v) / "backup_manifest"), target],
                   check=False)
        return p.returncode == 0

    def corrupt_and_check(self):
        v = sorted(int(p.name[1:]) for p in self.root.iterdir())[-1]
        copy = self.work / "bb-corrupt"
        rmrf(copy)
        shutil.copytree(self.dir(v), copy)
        off = flip_byte(copy / "base.tar.zst")
        tgt = self.work / "bb-corrupt-restore"
        rmrf(tgt)
        os.makedirs(tgt, mode=0o700)
        _, pt = run(["tar", "-I", "zstd", "-xf", str(copy / "base.tar.zst"), "-C", str(tgt)],
                    check=False)
        detected_how = "tar/zstd decode error" if pt.returncode != 0 else None
        if pt.returncode == 0:
            _, pv = run(["pg_verifybackup", "-n", "-m", str(copy / "backup_manifest"), str(tgt)],
                        check=False)
            if pv.returncode != 0:
                detected_how = "pg_verifybackup after extraction"
        rmrf(copy)
        rmrf(tgt)
        return {"detected": detected_how is not None, "how": detected_how, "offset": off}


class PgBackRestTool(Tool):
    def __init__(self, work, repo_no, block, name):
        super().__init__(work)
        self.repo_no = repo_no
        self.block = block
        self.name = name
        self.labels = {}

    @staticmethod
    def conf_path(work):
        return work / "pgbackrest" / "pgbackrest.conf"

    @staticmethod
    def write_conf(work, cluster):
        d = work / "pgbackrest"
        rmrf(d)
        for sub in ("repo1", "repo2", "log", "lock", "spool"):
            (d / sub).mkdir(parents=True)
        conf = f"""[global]
repo1-path={d}/repo1
repo1-retention-full=999
repo2-path={d}/repo2
repo2-retention-full=999
repo2-bundle=y
repo2-block=y
compress-type=zst
compress-level=3
process-max=4
start-fast=y
archive-copy=y
archive-check=y
log-level-console=warn
log-level-file=off
log-path={d}/log
lock-path={d}/lock
spool-path={d}/spool

[bench]
pg1-path={cluster.datadir}
pg1-port={cluster.port}
pg1-socket-path={cluster.sockdir}
"""
        PgBackRestTool.conf_path(work).write_text(conf)

    def cmd(self, *args):
        return ["pgbackrest", f"--config={self.conf_path(self.work)}", "--stanza=bench", *args]

    def backup(self, v, cluster):
        typ = "full" if v == 1 else "incr"
        dt, _ = run(self.cmd(f"--repo={self.repo_no}", f"--type={typ}", "backup"))
        _, p = run(self.cmd(f"--repo={self.repo_no}", "--output=json", "info"))
        info = json.loads(p.stdout)
        self.labels[v] = info[0]["backup"][-1]["label"]
        return dt

    def repo(self):
        return self.work / "pgbackrest" / f"repo{self.repo_no}"

    def size(self):
        return du(self.repo() / "backup")

    def size_detail(self):
        return {"backup": du(self.repo() / "backup"), "wal_archive": du(self.repo() / "archive")}

    def restore(self, v, target):
        os.makedirs(target, mode=0o700)
        dt, _ = run(self.cmd(f"--repo={self.repo_no}", f"--set={self.labels[v]}",
                             "--type=immediate", "--target-action=promote",
                             f"--pg1-path={target}", "restore"))
        return dt

    def corrupt_and_check(self):
        # corrupt a copy of the repository, verify it
        copy_root = self.work / f"pgbr-corrupt-{self.repo_no}"
        rmrf(copy_root)
        shutil.copytree(self.repo(), copy_root)
        f = largest_file(copy_root / "backup")
        off = flip_byte(f)
        conf = self.conf_path(self.work).read_text().replace(str(self.repo()), str(copy_root))
        cpath = self.work / "pgbackrest" / f"corrupt{self.repo_no}.conf"
        cpath.write_text(conf)
        _, p = run(["pgbackrest", f"--config={cpath}", "--stanza=bench", "--log-level-console=info",
                    f"--repo={self.repo_no}", "verify"], check=False, timeout=1800)
        rmrf(copy_root)
        out = p.stdout.decode() + p.stderr.decode()
        # verify exits 0 even when it finds damage; the verdict is in the report
        invalid = out.count("status: invalid")
        return {"detected": p.returncode != 0 or "status: error" in out or invalid > 0,
                "how": "pgbackrest verify (exit code 0, damage only in report)",
                "exit_code": p.returncode,
                "file": str(f.relative_to(copy_root)), "offset": off,
                "versions_affected": f"{invalid}/{len(self.labels)}"}


class StagedTool(Tool):
    """Tools that back up a plain-format pg_basebackup copy (restic, git)."""
    stage: Path


class ResticTool(StagedTool):
    name = "restic (on plain basebackup)"

    def __init__(self, work, stage):
        super().__init__(work)
        self.stage = stage
        self.repo = work / "restic"
        self.env = dict(ENV, RESTIC_REPOSITORY=str(self.repo), RESTIC_PASSWORD="bench-only",
                        RESTIC_CACHE_DIR=str(work / "restic-cache"))
        self.snaps = {}

    def setup(self, cluster):
        rmrf(self.repo)
        run(["restic", "init"], env=self.env)

    def backup(self, v, cluster):
        dt, _ = run(["restic", "backup", "--compression", "auto", "--tag", f"v{v}", "-q",
                     str(self.stage)], env=self.env)
        _, p = run(["restic", "snapshots", "--json", "--tag", f"v{v}"], env=self.env)
        self.snaps[v] = json.loads(p.stdout)[-1]["short_id"]
        return dt

    def size(self):
        return du(self.repo)

    def restore(self, v, target):
        os.makedirs(target, mode=0o700)
        dt, _ = run(["restic", "restore", f"{self.snaps[v]}:{self.stage}", "--target", target],
                    env=self.env)
        return dt

    def corrupt_and_check(self):
        copy = self.work / "restic-corrupt"
        rmrf(copy)
        shutil.copytree(self.repo, copy)
        f = largest_file(copy / "data")
        off = flip_byte(f)
        env = dict(self.env, RESTIC_REPOSITORY=str(copy))
        _, p = run(["restic", "check", "--read-data"], env=env, check=False)
        rmrf(copy)
        return {"detected": p.returncode != 0, "how": "restic check --read-data", "offset": off}


class GitTool(StagedTool):
    name = "git (on plain basebackup)"

    def __init__(self, work, stage):
        super().__init__(work)
        self.stage = stage
        self.gitdir = work / "gitrepo.git"
        self.gc_seconds = None

    def g(self, *args, worktree=None):
        cmd = ["git", f"--git-dir={self.gitdir}"]
        if worktree:
            cmd.append(f"--work-tree={worktree}")
        return cmd + list(args)

    def setup(self, cluster):
        rmrf(self.gitdir)
        run(["git", "init", "-q", "--bare", str(self.gitdir)])
        run(self.g("config", "core.compression", "3"))

    def backup(self, v, cluster):
        # git cannot store empty directories; PostgreSQL needs them -> placeholders
        for dirpath, dirs, files in os.walk(self.stage):
            if not dirs and not files:
                Path(dirpath, ".pgit-keep").touch()
        t0 = time.time()
        run(self.g("add", "-A", ".", worktree=self.stage), cwd=self.stage)
        run(self.g("-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-q",
                   "--allow-empty", "-m", f"v{v}", worktree=self.stage), cwd=self.stage)
        run(self.g("tag", "-f", f"v{v}"))
        return time.time() - t0

    def gc(self):
        dt, _ = run(self.g("gc", "-q"), timeout=7200)
        self.gc_seconds = dt
        return dt

    def size(self):
        return du(self.gitdir)

    def restore(self, v, target):
        os.makedirs(target, mode=0o700)
        t0 = time.time()
        run(f"git --git-dir={self.gitdir} archive v{v} | tar -x -C {target}", shell=True)
        run(f"find {target} -name .pgit-keep -delete; chmod -R u=rwX,go= {target}", shell=True)
        return time.time() - t0

    def corrupt_and_check(self):
        copy = self.work / "git-corrupt.git"
        rmrf(copy)
        shutil.copytree(self.gitdir, copy)
        packs = list((copy / "objects" / "pack").glob("*.pack"))
        f = max(packs, key=lambda p: p.stat().st_size) if packs else largest_file(copy / "objects")
        off = flip_byte(f)
        _, p = run(["git", f"--git-dir={copy}", "fsck", "--full"], check=False, timeout=3600)
        rmrf(copy)
        return {"detected": p.returncode != 0, "how": "git fsck --full", "offset": off}


class PgDumpTool(Tool):
    name = "pg_dump (logical, zstd)"

    def __init__(self, work):
        super().__init__(work)
        self.root = work / "pgdump"
        self.target_cluster = None

    def setup(self, cluster):
        rmrf(self.root)
        self.root.mkdir()

    def backup(self, v, cluster):
        dt, _ = run(["pg_dump", "-d", cluster.conn, "-Fc", "--compress=zstd:3",
                     "-f", str(self.root / f"v{v}.dump")])
        return dt

    def size(self):
        return du(self.root)

    def restore(self, v, target):
        # restore into a freshly initdb'd cluster (initdb not counted)
        run(["initdb", "-D", target, "-A", "trust", "--no-sync"])
        c = Cluster(target, 5442, self.work / "sock")
        c.start("-c archive_mode=off")
        try:
            c.psql("CREATE DATABASE bench", db="postgres")
            dt, _ = run(["pg_restore", "-j", "4", "-d", c.conn, str(self.root / f"v{v}.dump")])
        finally:
            c.stop()
        return dt


# ----------------------------------------------------------------- driver
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--scale", type=int, default=30)
    ap.add_argument("--versions", type=int, default=10)
    ap.add_argument("--txns", type=int, default=2500, help="pgbench transactions per client per step")
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--skip", default="", help="comma separated: git,restic,pgbackrest,dump,variants")
    ap.add_argument("--out", default=str(REPO_ROOT / "bench" / "results" / "results.json"))
    a = ap.parse_args()
    skip = set(filter(None, a.skip.split(",")))
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    sock = work / "sock"
    sock.mkdir(exist_ok=True)
    rng = random.Random(1234)

    data = work / "data"
    for p in [data, work / "restores"]:
        rmrf(p)
    (work / "restores").mkdir()
    primary = Cluster(data, 5440, sock)

    log(f"initdb (scale {a.scale}, {a.versions} versions)")
    run(["initdb", "-D", str(data), "-A", "trust", "--data-checksums", "--no-sync"])
    PgBackRestTool.write_conf(work, primary)
    with open(data / "postgresql.conf", "a") as f:
        f.write(f"""
wal_level = replica
max_wal_senders = 10
wal_keep_size = '2GB'
max_wal_size = '4GB'
shared_buffers = '512MB'
archive_mode = on
archive_command = 'pgbackrest --config={PgBackRestTool.conf_path(work)} --stanza=bench archive-push %p'
""")
    if "pgbackrest" in skip:
        with open(data / "postgresql.conf", "a") as f:
            f.write("archive_mode = off\n")
    primary.start()
    primary.psql("CREATE DATABASE bench", db="postgres")
    run(["pgbench", "-i", "-q", "-s", str(a.scale), "-h", str(sock), "-p", "5440", "bench"],
        timeout=7200)
    primary.psql("CREATE TABLE events (id bigserial primary key, at timestamptz default now(), "
                 "kind int, payload jsonb)")

    stage = work / "stage"
    bb = BaseBackupTool(work)
    pgit = PgitTool(work)
    tools: list[Tool] = [pgit, bb]
    if "variants" not in skip:
        tools += [PgitTool(work, "none", name="pgit-dedup-only", from_tar_of=bb),
                  PgitTool(work, "xor", name="pgit-xor-only", from_tar_of=bb),
                  PgitTool(work, "zdict", name="pgit-zdict-only", from_tar_of=bb),
                  PgitTool(work, "auto", name="pgit-auto-from-tar", from_tar_of=bb),
                  PgitTool(work, "auto", max_depth=2, name="pgit-auto-depth2", from_tar_of=bb)]
    if "pgbackrest" not in skip:
        run(["pgbackrest", f"--config={PgBackRestTool.conf_path(work)}", "--stanza=bench",
             "stanza-create"])
        tools += [PgBackRestTool(work, 1, False, "pgBackRest (file incr, zst)"),
                  PgBackRestTool(work, 2, True, "pgBackRest (block incr, zst)")]
    staged = []
    if "restic" not in skip:
        staged.append(ResticTool(work, stage))
    if "git" not in skip:
        staged.append(GitTool(work, stage))
    tools += staged
    if "dump" not in skip:
        tools.append(PgDumpTool(work))
    for t in tools:
        t.setup(primary)

    steps = workload_steps(primary, a.scale, a.txns, a.clients, rng)
    results = {"config": vars(a), "pg_version": primary.psql("SHOW server_version"),
               "versions": [], "restores": [], "corruption": {}}
    results["config"]["tools"] = [t.name for t in tools]

    for v in range(1, a.versions + 1):
        if v == 1:
            step = f"pgbench -i -s {a.scale}"
        else:
            step = steps[(v - 2) % len(steps)]()
        primary.psql("CHECKPOINT")
        sums = primary.checksums()
        db_size = int(primary.psql("SELECT sum(pg_database_size(oid)) FROM pg_database"))
        log(f"v{v}: {step}; cluster {db_size / 2**20:.0f} MiB")
        entry = {"v": v, "step": step, "checksums": sums, "db_bytes": db_size, "backups": {}}
        if staged:
            rmrf(stage)
            dt_stage, _ = run(["pg_basebackup", "-d", primary.conn, "-D", str(stage), "-F", "plain",
                               "-X", "fetch", "-c", "fast", "--no-sync", "-l", f"stage-v{v}"])
            entry["stage_seconds"] = dt_stage
        for t in tools:
            try:
                dt = t.backup(v, primary)
                entry["backups"][t.name] = {"seconds": round(dt, 3), "repo_bytes": t.size(),
                                            **t.size_detail()}
                log(f"   {t.name:<34} {dt:7.2f}s  repo {t.size() / 2**20:9.1f} MiB")
            except Exception as e:  # noqa: BLE001
                entry["backups"][t.name] = {"error": str(e)[:2000]}
                log(f"   {t.name}: ERROR {str(e)[:300]}")
        results["versions"].append(entry)
        save(results, a.out)

    for t in tools:
        if isinstance(t, GitTool):
            log("git gc ...")
            try:
                dt = t.gc()
                results["git_gc"] = {"seconds": round(dt, 2), "repo_bytes": t.size()}
                log(f"   git gc {dt:.1f}s -> {t.size() / 2**20:.1f} MiB")
            except Exception as e:  # noqa: BLE001
                results["git_gc"] = {"error": str(e)[:1000]}

    # pgit repository statistics
    _, p = run(PGIT + ["-C", str(pgit.repo), "stats", "--json"])
    results["pgit_stats"] = json.loads(p.stdout)
    results["pgit_backup_stats"] = pgit.stats
    _, p = run(PGIT + ["-C", str(pgit.repo), "diff", "--json", "v1", f"v{a.versions}"])
    results["pgit_diff_first_last"] = json.loads(p.stdout)

    # ------------------------------------------------------------ restores
    primary.stop()
    mid = (a.versions + 1) // 2
    plan = []
    for t in tools:
        if not t.restorable:
            continue
        vs = list(range(1, a.versions + 1)) if t is pgit else sorted({1, mid, a.versions})
        for v in vs:
            plan.append((t, v))
    expected = {e["v"]: e["checksums"] for e in results["versions"]}
    for t, v in plan:
        target = str(work / "restores" / "r")
        rmrf(target)
        rec = {"tool": t.name, "v": v}
        try:
            rec["restore_seconds"] = round(t.restore(v, target), 3)
            if isinstance(t, PgDumpTool):
                c = Cluster(target, 5442, sock)
                c.start("-c archive_mode=off")
                rec["startup_seconds"] = 0.0
            else:
                if t is pgit:
                    _, pv = run(["pg_verifybackup", "-n", target], check=False)
                    rec["pg_verifybackup"] = pv.returncode == 0
                elif isinstance(t, BaseBackupTool):
                    rec["pg_verifybackup"] = t.verify_restored(v, target)
                c = Cluster(target, 5441, sock)
                rec["startup_seconds"] = round(c.start("-c archive_mode=off"), 3)
            got = c.checksums()
            rec["checksums_match"] = got == expected[v]
            if not rec["checksums_match"]:
                rec["checksum_diff"] = {k: (expected[v].get(k), got.get(k))
                                        for k in set(got) | set(expected[v])
                                        if expected[v].get(k) != got.get(k)}
            if v == a.versions:
                dt, pa = run(["pg_amcheck", "-h", str(sock), "-p", str(c.port), "--install-missing",
                              "--heapallindexed", "-d", "bench"], check=False, timeout=3600)
                rec["pg_amcheck"] = pa.returncode == 0
                rec["pg_amcheck_seconds"] = round(dt, 2)
            c.stop()
            if t is pgit and not isinstance(t, PgDumpTool):
                _, pc = run(["pg_checksums", "--check", "-D", target], check=False)
                rec["pg_checksums"] = pc.returncode == 0
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:2000]
            run(["pg_ctl", "-D", target, "-m", "immediate", "stop"], check=False)
        rec["restored_bytes"] = du(target) if os.path.exists(target) else None
        log(f"restore {t.name:<34} v{v:<3} {rec.get('restore_seconds', '-')}s + start "
            f"{rec.get('startup_seconds', '-')}s  match={rec.get('checksums_match')}"
            f" {('ERR ' + rec['error'][:200]) if 'error' in rec else ''}")
        results["restores"].append(rec)
        save(results, a.out)
        rmrf(target)

    # pgit in-place delta restore: switch a restored v_last directory to v1 and back
    target = str(work / "restores" / "delta")
    rmrf(target)
    run(PGIT + ["-C", str(pgit.repo), "restore", f"v{a.versions}", target])
    dt1, p1 = run(PGIT + ["-C", str(pgit.repo), "restore", "--delta", "--json", "v1", target])
    c = Cluster(target, 5441, sock)
    c.start("-c archive_mode=off")
    ok1 = c.checksums() == expected[1]
    c.stop()
    dt2, p2 = run(PGIT + ["-C", str(pgit.repo), "restore", "--delta", "--json",
                          f"v{a.versions}", target])
    c.start("-c archive_mode=off")
    ok2 = c.checksums() == expected[a.versions]
    c.stop()
    results["pgit_delta_restore"] = {
        "last_to_first": {"seconds": round(dt1, 3), **json.loads(p1.stdout), "checksums_match": ok1},
        "first_to_last": {"seconds": round(dt2, 3), **json.loads(p2.stdout), "checksums_match": ok2}}
    log(f"pgit delta restore v{a.versions}->v1 {dt1:.2f}s ok={ok1}; v1->v{a.versions} {dt2:.2f}s ok={ok2}")
    rmrf(target)

    # pgit verify / fsck timing
    dt, _ = run(PGIT + ["-C", str(pgit.repo), "fsck"])
    results["pgit_fsck_seconds"] = round(dt, 2)
    dt, _ = run(PGIT + ["-C", str(pgit.repo), "verify", "--all"])
    results["pgit_verify_all_seconds"] = round(dt, 2)

    # -------------------------------------------------------- corruption
    for t in tools:
        if not t.restorable or isinstance(t, PgDumpTool):
            continue
        try:
            r = t.corrupt_and_check()
        except Exception as e:  # noqa: BLE001
            r = {"error": str(e)[:1000]}
        results["corruption"][t.name] = r
        log(f"corruption {t.name:<34} detected={r.get('detected')} {r.get('how', '')}")
    save(results, a.out)
    log("done")


def save(results, out):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    results["log"] = LOG
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(results, f, indent=1)
    os.replace(tmp, out)


if __name__ == "__main__":
    main()
