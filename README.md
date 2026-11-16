# pgit: git-style versioned backups for PostgreSQL

pgit keeps every backup of a PostgreSQL cluster as a **commit** in a
content-addressed object store. The first backup is a full base backup. Each
later one stores only the 8 KiB pages that changed, as small **XOR or
zstd-dictionary deltas** against the same page in the previous version. Any
historical version can be rebuilt byte for byte, verified, diffed by table,
or checked out in place.

```
$ pgit -C /backups/repo init
$ pgit -C /backups/repo backup -d "host=db1 user=replicator" -l nightly
commit 1ea9c73d9f44  @2  nightly
  985 files, 491.6 MiB logical, 62932 blocks: 47063 unchanged, 2047 dedup, 13822 new
  stored 2.5 MiB (objects: zstd=71, xor=2125, zdict=11061, tdict=798) in 3.7s

$ pgit -C /backups/repo log
$ pgit -C /backups/repo diff HEAD~1 HEAD          # which tables changed, in blocks
$ pgit -C /backups/repo restore HEAD~3 /srv/pg/restored     # any version
$ pg_ctl -D /srv/pg/restored start                          # recovers from its own WAL
$ pgit -C /backups/repo restore --delta HEAD /srv/pg/restored   # roll forward in place
$ pgit -C /backups/repo fsck                      # hash-check every object
```

## Results (pgbench scale 50, 12 versions, PostgreSQL 16)

Full tables and charts, generated from the raw benchmark output, are in
**[docs/RESULTS.md](docs/RESULTS.md)**. The design is in
**[docs/DESIGN.md](docs/DESIGN.md)**.

An 850 MB cluster changed 11 times (pgbench OLTP runs, bulk inserts, a 250k-row
update, index build, delete + vacuum, `VACUUM FULL`); every tool used zstd
level 3:

| | storage for all 12 versions | added per version (mean) | restore + start latest version | restored clusters correct |
|---|---:|---:|---:|:-:|
| **pgit** | **105 MiB** | **4.6 MiB** | 1.9 s + 0.9 s | 12/12 |
| pgBackRest 2.50, block incremental | 230 MiB | 17.5 MiB | 1.6 s + 0.3 s | 3/3 |
| pgBackRest 2.50, file incremental | 317 MiB | 25.3 MiB | 1.4 s + 0.3 s | 3/3 |
| restic 0.16 (on a plain base backup) | 283 MiB | 22.4 MiB | 1.7 s + 0.8 s | 3/3 |
| git 2.43 (on a plain base backup) | 388 MiB (249 after `git gc`) | 31.3 MiB | 1.9 s + 1.0 s | 3/3 |
| pg_basebackup, full every time | 611 MiB | 52.2 MiB | 1.4 s + 1.0 s | 3/3 |
| pg_dump -Fc (logical) | 144 MiB | 12.7 MiB | 5.1 s (pg_restore -j4) | 3/3 |

*Correct* means PostgreSQL started on the restored directory and an
order-independent hash of every row of every table equalled the value recorded
at backup time. `pg_amcheck --heapallindexed` passed on every tool's latest
restore, and `pg_verifybackup` and `pg_checksums` passed on every pgit restore.

* **Storage.** pgit is 2.2× smaller than pgBackRest's block incremental and 5.8×
  smaller than keeping full base backups. On a pgbench round it adds ~4.5 MiB
  where pgBackRest adds 30–46 MiB, because it stores 8 KiB pages as XOR /
  dictionary deltas. On append-only changes (bulk insert, a table rewrite)
  all tools pay about the same, because the data is new.
* **Ablation.** Dedup alone (whole changed pages) needs 179 MiB, XOR-only 116,
  zdict-only 105, and auto (the default) 104.5. Capping delta chains at 2
  costs +9%.
* **Where pgit loses.** The *first* backup is 54 MiB against ~37 MiB for the
  others: per-page addressing costs 32-byte ids in block lists plus an index
  row per page, and pages are compressed independently (a trained zstd
  dictionary recovers most, not all, of the streaming ratio). pgit is ahead
  from the second version on.
* **Restore.** Restores run at ~450 MB/s, and later versions take longer as delta
  chains grow (1.2 s for v1 → 1.9 s for v12, bounded by `max_depth`). The
  slower *startup* of pgit, pg_basebackup, restic and git comes from crash
  recovery from `backup_label`, which ends with a synchronous checkpoint.
  pgBackRest restores into archive recovery and promotes, which defers that
  checkpoint. `restore --delta` switches an existing v12 directory back to v1
  in 0.8 s.
* **Integrity.** Every tool detected a flipped byte in its repository
  (`pgbackrest verify` reports it but still exits 0). pgit names the damaged
  objects. Because pages are shared across versions, one damaged base page
  affected all 12 versions in this test, so keep the repository on redundant
  storage.

## How it works (short version)

* **Source.** `pg_basebackup -D - -Ft -X fetch` is streamed straight into the
  store, with no staging copy. Every snapshot carries the WAL it needs to be
  consistent and PostgreSQL's own `backup_manifest`.
* **Dedup.** Files are cut into 8 KiB blocks, one per PostgreSQL page, and
  identified by SHA-256. An unchanged page costs nothing; a page seen anywhere
  before is stored once.
* **Deltas.** A changed page is delta-encoded against the page at the same
  position in the parent snapshot, keeping whichever is smallest: XOR+zstd
  (in-place changes such as updated tuples and LSNs), zstd with the old page as
  dictionary (shifted content such as B-tree splits), or the whole page with a
  zstd dictionary trained on the repository's first pages. Delta chains are
  capped (`--max-depth`, default 8).
* **Merkle DAG.** commit → tree → blocklists → pages, all addressed by hash.
  Every read is verified, `fsck` checks everything, and the index can be
  rebuilt from the append-only pack files alone (`reindex`).
* **Restore** uses forked worker processes (about 450 MB/s here), writes a
  normal base-backup directory that PostgreSQL recovers on its own, and
  `--delta` rewrites only differing pages of an existing directory.
* **Retention.** `rm` + `gc` repack. Pages whose delta base disappears are
  re-delta'd against the nearest surviving older version.

## Commands

| command | |
|---|---|
| `init [--delta auto\|xor\|zdict\|none] [--max-depth N] [--level N]` | create a repository |
| `backup -d CONNINFO \| --from-dir DIR \| --from-tar FILE\|-  [-l LABEL] [-t TAG]` | take a snapshot |
| `log`, `show [REV]`, `stats` | inspect history and storage |
| `diff [FROM] [TO]` | block-level changes grouped by relation (`schema.table [fork]`) |
| `restore REV DIR [-j N] [--delta] [--fsync]` | rebuild a version |
| `verify [REV \| --all]`, `fsck` | integrity checks |
| `tag NAME [REV]`, `rm REV... [--gc]`, `gc`, `reindex` | refs, retention, repair |

`REV` is `HEAD`, `HEAD~N`, `@SEQ`, a tag, a backup label or a commit-id prefix.

## Install and run

```
pip install .            # needs Python >= 3.10, zstandard, PostgreSQL client tools
pgit --help
```

Tests:

```
python -m pytest tests                     # store / snapshot / restore / gc / reindex, no server needed
su postgres -c "python3 bench/run_bench.py --work /scratch/bench --scale 50 --versions 12"
python3 bench/report.py                    # -> docs/RESULTS.md + docs/img/*.svg
```

The benchmark needs PostgreSQL 16 server binaries, pgBackRest, restic, git and
zstd. It must run as a non-root user, because PostgreSQL refuses to run as root.

## Limitations

The streaming mode supports the default tablespace only (a `pg_basebackup`
restriction for stdout); there is no point-in-time recovery between snapshots
(pair pgit with a WAL archive for that); and every backup still reads the whole
cluster, so the savings are in storage, not in read I/O. See
[docs/DESIGN.md §7](docs/DESIGN.md#7-limitations-and-next-steps).
