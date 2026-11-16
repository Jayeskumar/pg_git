# pgit design

pgit stores PostgreSQL physical backups the way git stores source trees. Every
snapshot is a complete, independently restorable commit. Under the hood, the
first snapshot is effectively a full base backup and every later one costs only
the pages that changed, stored as small deltas.

```
                commit @3 ──parent──▶ commit @2 ──parent──▶ commit @1
                   │                     │                     │
                  tree                  tree                  tree        (JSON: path, mode, size,
                   │                     │                     │           SHA-256, block list)
      base/16384/16397 ...   base/16384/16397 ...   base/16384/16397 ...
          │ │ │ │                 │ │ │ │                │ │ │ │
          ▼ ▼ ▼ ▼                 ▼ ▼ ▼ ▼                ▼ ▼ ▼ ▼
   blob blob Δ blob ...    blob  Δ  blob blob ...  blob blob blob blob ...  (8 KiB pages, by SHA-256)
               │                 │
               └───── delta base: the same page in the parent snapshot
```

## 1. Getting a consistent copy

`pgit backup -d <conninfo>` runs

```
pg_basebackup -D - -F tar -X fetch -c fast --manifest-checksums=CRC32C
```

and parses the tar stream as it arrives (`tarfile` stream mode), so nothing is
staged on disk. That gives us PostgreSQL's own guarantees for free:

* the copy is taken under `pg_backup_start/stop` over the replication
  protocol, so it works against a remote server;
* `-X fetch` puts every WAL segment needed to make the copy consistent into
  `pg_wal/`, which makes each snapshot self-contained. No WAL archive is needed
  to restore it;
* when writing to stdout, `pg_basebackup` appends its `backup_manifest` to the
  stream. pgit stores it like any other file, so a restored directory can be
  checked with the stock `pg_verifybackup`.

`--from-dir` (a stopped cluster or a `-Fp` base backup) and `--from-tar`
(a `-Ft` archive or stdin) ingest other sources. At backup time pgit also asks
the catalog of every database for `pg_relation_filepath()` → relation name, so
that `pgit diff` can say *which tables* changed.

## 2. Object store

All data lives in an append-only **object store**: `packs/pack-NNNNNN.pack`
plus an SQLite index (`index.sqlite`).

* **Identity.** An object's id is the SHA-256 of its raw content. Files are
  cut into 8 KiB blocks, which line up with PostgreSQL pages (heap, index, FSM,
  VM, SLRU and WAL pages are all 8 KiB). A page that did not change between
  snapshots, or that appears twice (all-zero pages, identical WAL pages), is
  stored once. This is the deduplication layer.
* **Encodings.** How an object is stored is independent of its identity,
  as with git's loose objects vs. packfile deltas:

  | kind  | payload | used for |
  |-------|---------|----------|
  | raw   | the bytes | incompressible data |
  | zstd  | zstd(raw) | non-page objects (trees, commits) |
  | tdict | zstd with a **trained dictionary** | full pages |
  | xor   | zstd(raw ⊕ base) | in-place page changes (heap tuples updated in place, hint bits, LSN) |
  | zdict | zstd with `base` as a raw-content dictionary | shifted content (B-tree splits, line-pointer changes, compaction) |

  The delta base hint for block *i* of a file is block *i* of the same file in
  the parent snapshot. In `auto` mode pgit computes both deltas and keeps the
  smallest of {full, xor, zdict}, provided the delta is at least 10% smaller.
  On real pgbench pages XOR wins on heap pages and zdict on B-tree pages
  (`bench/` shows both are needed).
* **Deltas refer to raw content**, never to the base's encoding, so a base can
  be re-encoded (by gc) without touching what depends on it.
* **Bounded chains.** A delta's depth is base depth + 1. When a chain reaches
  `max_depth` (default 8) the page is stored in full again, which bounds the
  work of reconstructing any page of any version.
* **Trained dictionary.** Compressing 8 KiB pages one at a time loses the
  redundancy *between* pages that a streaming compressor exploits (about 45 MB
  vs 26 MB on a pgbench cluster). pgit holds back the first 4096 new pages of a
  repository, trains a 110 KiB zstd dictionary on them, stores it as an object,
  and compresses full pages against it. That recovers most of the gap (28 MB).
  A `tdict` record names its dictionary just as a delta names its base, so gc
  and reindex need no special cases.
* **Pack records are self-describing**: `'P' | type<<4|kind | rawlen | paylen |
  [base id] | payload`. The delta base and dictionary ids live only in the
  record header, not in the index (which saves 32 bytes per row), and the whole
  index can be rebuilt from the packs (`pgit reindex`). Bases are always
  written before their dependants, so a single sequential scan can decode and
  hash every record.
* **Crash safety.** The pack is `fsync`ed before index rows that point into it
  are committed, and the commit row is written last. A crash mid-backup leaves
  unreferenced bytes at the end of a pack and nothing else.

## 3. Trees and commits

A **tree** is a JSON list of entries `{path, type, mode, size, sha256, blocks}`
covering directories (including the empty ones PostgreSQL needs), symlinks and
files. Small files list their block ids inline. Large files point to
**blocklist** objects of 256 ids (8 KiB each), so unchanged ranges of a
1 GiB relation segment cost nothing in the next tree. Blocklists and trees are
delta-encoded against their predecessors like pages are.

A **commit** holds the tree id, the parent id, the time and label, the
`backup_label` contents (start LSN, timeline, checkpoint), the system identifier
from `pg_control`, the relation map, and the ingest statistics. Its id is the
hash of all that, so commit → tree → blocklist → page forms a Merkle DAG.
Verifying every object's hash on read verifies the whole snapshot.

Revisions can be named `HEAD`, `HEAD~N`, `@SEQ`, a tag (`pgit tag`), the
backup label, or a hex prefix of the commit id.

## 4. Restore

`pgit restore REV DIR`:

1. loads the object index into memory and walks the tree;
2. creates directories and symlinks, and pre-sizes every file;
3. splits files into 8 MiB segments and reconstructs them in a pool of
   **forked worker processes**. Decoding many small pages is CPU- and
   GIL-bound, so threads made restore *slower*: 1.9 s → 5.6 s going from 1 to
   4 threads, while 4 processes take 1.2 s. Workers share the preloaded index
   copy-on-write and only `pread()` the packs;
4. checks every page's SHA-256 while writing (`--no-verify` skips this);
5. sets directory modes last.

The result is a normal base backup directory with `backup_label` and the WAL
it needs. Starting PostgreSQL on it runs crash recovery to the end of that WAL
and the cluster is consistent. No `restore_command` or recovery target is
needed.

`--delta` updates an existing directory in place. It hashes the pages already
there, rewrites only those that differ, and deletes files that are not in the
snapshot. Switching a cluster between two versions therefore costs only their
difference.

## 5. Integrity

| check | what it proves |
|---|---|
| hash on every read | the page / blocklist / tree / commit is exactly what was stored |
| `pgit verify [--all]` | each file of a snapshot rebuilds to its recorded size and whole-file SHA-256 |
| `pgit fsck` | every object in every pack decodes and hashes correctly, and every object reachable from a commit exists |
| `pg_verifybackup` on a restore | the restored files match PostgreSQL's own `backup_manifest` (an independent check computed by the server) |
| start + `pg_amcheck` | the cluster recovers and its heap/indexes are structurally sound |

Content addressing has a known downside: a page shared by many snapshots is
stored once, so damage to it affects every snapshot that uses it (the "blast
radius", see `RESULTS.md`). pgit always *detects* such damage. It cannot repair
it from one copy, so keep the repository on redundant storage or replicate
the packs. Packs are append-only and immutable, which makes them easy to
`rsync` or upload to object storage.

## 6. Retention and garbage collection

`pgit rm REV` forgets a snapshot and `pgit gc` repacks. Live objects whose base
is still live are copied verbatim. A live object whose base died is
re-delta'd against the *nearest surviving ancestor* in its old chain (for
example the same page two snapshots back), or stored in full if there is none.
Packs are rewritten, the index is swapped in one transaction, and the old packs
are deleted afterwards.

## 7. Limitations and next steps

* Only the default tablespace is supported in `-d` (streaming) mode, because
  `pg_basebackup` refuses to write several tablespaces to stdout. `--from-dir`
  works with any layout.
* Every backup reads the whole cluster: incrementality is in *storage*, not
  in I/O. PostgreSQL 17's `pg_basebackup --incremental` (WAL summaries) would
  let pgit fetch only changed blocks and splice them onto the parent tree,
  which fits this design directly.
* No PITR: pgit snapshots are consistent points. Combining it with a WAL
  archive (for example `pg_receivewal`) gives PITR between snapshots.
* Forward deltas make newer versions slightly slower to rebuild than older
  ones, up to `max_depth`. git uses reverse deltas (newest full) to favour the
  latest version. A `gc --reverse` that re-encodes chains newest-first is a
  natural extension.
* The per-page index costs ~60 bytes per stored page. It is not significant
  for incremental snapshots, but it shows on the very first backup of a highly
  compressible cluster.
