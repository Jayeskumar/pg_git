"""Talking to PostgreSQL: streaming base backups and catalog lookups."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from contextlib import contextmanager

RELMAP_SQL = (
    "SELECT pg_relation_filepath(c.oid), n.nspname || '.' || c.relname, c.relkind "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r','i','t','m','S','p','I') AND pg_relation_filepath(c.oid) IS NOT NULL")


def pg_bin(name: str) -> str:
    """Locate a PostgreSQL client binary (PATH, then $PG_BINDIR, then distro dirs)."""
    env = os.environ.get("PG_BINDIR")
    if env and os.path.exists(os.path.join(env, name)):
        return os.path.join(env, name)
    found = shutil.which(name)
    if found:
        return found
    for base in ("/usr/lib/postgresql", "/usr/pgsql"):
        if os.path.isdir(base):
            for v in sorted(os.listdir(base), reverse=True):
                cand = os.path.join(base, v, "bin", name)
                if os.path.exists(cand):
                    return cand
    raise FileNotFoundError(f"{name} not found; set PG_BINDIR")


def _with_db(conn: str, dbname: str) -> str:
    if conn.startswith(("postgres://", "postgresql://")):
        sep = "&" if "?" in conn else "?"
        return f"{conn}{sep}dbname={dbname}"
    return f"{conn} dbname={dbname}"


def psql(conn: str, sql: str) -> list[list[str]]:
    out = subprocess.run([pg_bin("psql"), "-X", "-q", "-A", "-t", "-F", "\t", "-d", conn, "-c", sql],
                         check=True, capture_output=True, text=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def catalog_maps(conn: str) -> tuple[dict, dict]:
    """Return (relmap, dbmap): relation file path -> 'schema.rel (kind)', db oid -> name."""
    dbmap = {}
    relmap = {}
    for oid, name, allow in psql(conn, "SELECT oid, datname, datallowconn FROM pg_database"):
        dbmap[oid] = name
        if allow != "t":
            continue
        try:
            rows = psql(_with_db(conn, name), RELMAP_SQL)
        except subprocess.CalledProcessError:
            continue
        for path, rel, kind in rows:
            relmap[path] = f"{name}:{rel}" if not path.startswith("global/") else f"{rel}"
    return relmap, dbmap


@contextmanager
def basebackup_stream(conn: str, label: str = "pgit", extra_args: list[str] | None = None):
    """Run ``pg_basebackup`` writing a tar stream to stdout and yield that stream.

    WAL needed for consistency is fetched into the archive (``-X fetch``) and the
    backup manifest is injected at the end, so every snapshot is self-contained
    and verifiable with ``pg_verifybackup``.
    """
    cmd = [pg_bin("pg_basebackup"), "-d", conn, "-D", "-", "-F", "tar", "-X", "fetch",
           "-c", "fast", "-l", label, "--manifest-checksums=CRC32C"]
    cmd += extra_args or []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1 << 20)
    err = []
    t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    t.start()
    try:
        yield proc.stdout
        # drain any trailing padding so pg_basebackup can exit cleanly
        while proc.stdout.read(1 << 20):
            pass
    except BaseException:
        proc.kill()
        raise
    finally:
        rc = proc.wait()
        t.join()
    if rc != 0:
        raise RuntimeError(f"pg_basebackup failed ({rc}): {err[0].decode(errors='replace') if err else ''}")
