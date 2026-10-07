[![progress-banner](https://backend.codecrafters.io/progress/redis/f43a7cd3-11e6-45dc-a8ba-9d6690c73ac0)](https://app.codecrafters.io/users/anishchugh05?r=2qF)

# Aether Cache

**Shared memory for AI coding tools that stays true to your code.**

Claude Code, Cursor, and other AI tools each forget your project between sessions, and when they do remember things,
nothing checks whether those memories are still correct. Aether Cache gives every tool one shared, local memory, and keeps
it honest:

- **Memories go stale when the code changes.** Each memory is linked to the files it's about, with a snapshot of their
  git content hash. When `docker-compose.yml` changes, the memory about it is retired automatically instead of being
  fed to your AI forever.
- **Contradictions get replaced, not hoarded.** "We migrated to MySQL" retires "we use Postgres" instead of sitting
  next to it and confusing the model. Near-identical repeats are merged.
- **Every memory can explain itself.** Which tool and session saved it, which file versions it was based on, and every
  change since: created, seen again, went stale, confirmed, replaced.
- **It works with the files you already use.** Current memories sync into a managed section of `AGENTS.md` and
  `CLAUDE.md`, so they're committed, reviewed in PRs, and visible to teammates who don't run Aether Cache.

It runs entirely on your machine, on top of a database engine I wrote from scratch.

## Not a Redis clone

Aether Cache started as a from-scratch rebuild of Redis in Java, then kept going. Every layer here is written from
scratch, and each one does something the layer beneath it can't:

| Layer | What it adds |
|---|---|
| **MCP server** | Claude Code, Cursor, and any MCP tool share one memory |
| **Git awareness** | Memories retire themselves when the code they describe changes |
| **Memory layer** | Status, provenance, full history, duplicate merging, and conflict review, inside the engine |
| **HNSW vector index** | Semantic search: 99.4% recall at 0.84 ms on 100K vectors, 22× faster than exact search |
| **Redis-compatible engine** | RESP protocol, 50+ commands, replication, transactions, and crash-safe persistence (0 acknowledged writes lost across 1,039,890 writes and 8 `kill -9` crashes), at 88–93% of real Redis's throughput |

The bottom layer alone is a working Redis: `redis-cli`, `redis-benchmark`, and `redis-py` talk to it unchanged. The
memory layer, git awareness, and MCP server are things Redis doesn't do at all.

```
Claude Code ─┐                       ┌─────────────────────────────────────────────┐
Cursor ──────┼─ MCP ─► memory_mcp ───┼─ RESP ─► Java engine (Main.java)            │
other tools ─┘   (Python: embeddings,│          memories, HNSW vector index,       │
                  git hashing, sync) │          history, append-only file on disk  │
                                     └─────────────────────────────────────────────┘
```

## What it looks like

A memory linked to a file, the file changes, and the next lookup catches it:

```
> remember("The database runs Postgres via docker compose", files=["docker-compose.yml"])
Saved as m1.

        ... someone edits docker-compose.yml ...

> recall("database setup")
These memories were just retired because files they depend on changed. If one is still true,
call confirm_memory; if it's wrong, save the corrected fact with remember:
[m1] "The database runs Postgres via docker compose" is now marked outdated: docker-compose.yml changed.

> confirm_memory("m1")        # still true, so re-link it to the new version of the file
Confirmed m1; it's active again and linked to the current file versions.

> explain_memory("m1")
m1: "The database runs Postgres via docker compose"
status: active, project: demo
saved by: claude-code (session eea54b5c)
linked files: docker-compose.yml@02ea346...
history:
  19:16  created: by claude-code (session eea54b5c), linked to docker-compose.yml@87da641...
  19:16  active -> stale: docker-compose.yml changed since this was saved
  19:16  re-linked: to docker-compose.yml@02ea346... (confirmed still true)
  19:16  stale -> active: confirmed still true
```

A newer fact replacing an older one:

```
> remember("The API timeout was raised to 60 seconds")
Saved as m3.
Related existing memories:
[m1] (similarity 0.58) The API timeout is 30 seconds
If m3 makes any of these no longer true, call replace_memory(old_id, "m3"). Otherwise do nothing.

> replace_memory("m1", "m3")
m1 retired; m3 replaces it.
```

## Quick start

Requires Java 11+ and Python 3.10+.

```sh
# 1. Python environment for the MCP server
micromamba create -n aether-cache python=3.11 -y && micromamba activate aether-cache
pip install -r memory_mcp/requirements.txt

# 2. Start the engine, with memories saved to ~/.aether-cache (terminal 1)
java src/main/java/Main.java --dir ~/.aether-cache --appendonly yes

# 3. Connect it to Claude Code, for all projects (terminal 2)
claude mcp add aether-cache --scope user -e MEMORY_SOURCE=claude-code -- \
  $(which python) "$(pwd)/memory_mcp/server.py"
```

Start `claude` in any git repo and run `/mcp` to check that `aether-cache` is connected. Memories are kept per project
(the git repo's folder name), so projects never mix. Any other MCP-capable tool connects the same way; set
`MEMORY_SOURCE` to its name so you can tell which tool saved what.

## Tools the AI can call

| Tool | What it does |
|---|---|
| `remember(fact, files?)` | Saves a durable fact, optionally linked to files. Merges near-identical repeats and shows related memories so the AI can decide whether the new fact replaces one. |
| `recall(query, limit?)` | Searches the project's memories by meaning. Checks linked files first, so it never returns a memory whose code changed underneath it. |
| `explain_memory(id)` | Where a memory came from and its full history. |
| `replace_memory(old, new)` | Retires an older memory that a newer one makes untrue. |
| `mark_outdated(id, reason)` | Retires a memory that turned out to be wrong. |
| `confirm_memory(id)` | Brings back a memory that's still true after its files changed, re-linked to the current versions. |
| `check_stale()` | Runs the file check on demand. |
| `list_conflicts()` / `resolve_conflict(...)` | Reviews pairs of very similar memories that may contradict each other. |
| `sync_to_files()` | Writes current memories into `AGENTS.md` / `CLAUDE.md` (also: `python memory_mcp/server.py sync`). |

## Design decisions, and what the measurements said

### Similarity can't detect contradictions, so the AI judges

The first version flagged a new memory as a possible contradiction when it was similar to an existing one. Calibrating
with real `all-MiniLM-L6-v2` embeddings showed that can't work:

| Pair | Kind | Similarity |
|---|---|---|
| "we use Postgres" / "the database is Postgres" | duplicate | 0.73 |
| "tests need DOCKER_HOST set" / "you have to set DOCKER_HOST before running tests" | duplicate | 0.92 |
| "we use Postgres" / "we migrated to MySQL" | **contradiction** | **0.30** |
| "API timeout is 30 seconds" / "API timeout is 60 seconds" | **contradiction** | **0.94** |
| "tests need DOCKER_HOST set" / "integration tests need a running Redis" | related, both true | 0.34 |

Duplicates scored 0.73 to 0.92 and contradictions 0.30 to 0.94, so no threshold separates them. A threshold low enough
to merge real duplicates would silently merge "timeout is 30s" with "timeout is 60s", the worst bug this product could
have. So the engine only auto-merges near-identical text (similarity ≥ 0.97). For everything else, embeddings *find*
the related memories and the AI, which already understands the sentences, *decides* whether one replaces another.

### Staleness compares file contents, not commits

Each linked file is stored as `path@<git blob hash>`, computed with `git hash-object`. Comparing contents rather than
commit ids catches uncommitted edits, ignores commits that didn't touch the file, and needs no git hooks: `recall`
re-hashes the handful of linked files before every search.

### Memories never mutate silently

Every change is a logged event with a timestamp and a reason. The engine logs each memory *with* the id and timestamp
it assigned, so replaying the log after a restart rebuilds the exact same memories and histories, down to the
millisecond, instead of minting new ones.

## The engine underneath

A Redis-compatible server written from scratch in Java. It speaks RESP, so `redis-cli`, `redis-benchmark`, and
`redis-py` work with it unchanged. The core was built by completing every stage and extension of the
[CodeCrafters "Build Your Own Redis"](https://codecrafters.io/challenges/redis) challenge; the vector index,
persistence hardening, and the memory layer are my own additions.

### Throughput vs. official Redis

`redis-benchmark -t set,get`, 100K requests, 50 parallel clients, 3-byte values, same machine:

| | Aether Cache | Redis 8.10.2 | Aether Cache as % of Redis |
|---|---|---|---|
| SET | 205.8K ops/sec | 233.6K ops/sec | 88% |
| GET | 225.7K ops/sec | 242.7K ops/sec | 93% |
| p50 latency | 0.119 ms | 0.111 ms | |
| p99 latency (SET / GET) | 0.375 / 0.231 ms | 0.359 / 0.159 ms | |
| max latency (SET) | 19.7 ms | 1.3 ms | JVM GC / JIT pauses |

Typical latency is on par with Redis. The gap is in worst-case latency, from JVM garbage collection and JIT warm-up,
which Redis (written in C) doesn't have.

### HNSW vector search vs. exact search

100,000 AG News headlines embedded with `all-MiniLM-L6-v2` (384 dimensions, cosine similarity), 500 held-out queries,
k = 10. Recall is measured against exact brute-force search on the same data.

| Mode | Recall@10 | p50 latency | p99 latency |
|---|---|---|---|
| Exact (brute force) | 1.000 | 18.58 ms | 20.52 ms |
| HNSW, ef=10 | 0.905 | 0.49 ms | 1.38 ms |
| HNSW, ef=20 | 0.951 | 0.52 ms | 0.73 ms |
| HNSW, ef=50 | 0.985 | 0.66 ms | 0.89 ms |
| **HNSW, ef=100** | **0.994** | **0.84 ms** | **1.41 ms** |
| HNSW, ef=200 | 0.998 | 1.24 ms | 2.19 ms |

At ef=100, HNSW returns 99.4% of the true top-10 about **22× faster** than exact search. Building the index for 100K
vectors took 136 seconds (M=16, M0=32, efConstruction=200). Latencies are measured from the Python client, so they
include network and client overhead. Full setup and raw numbers: `results.json`.

**How it works:** each vector links to its nearest neighbors in a layered graph. A search starts on a sparse top layer
of long-range links, hops greedily toward the query, drops down a layer at a time, and finishes with a best-first search
on the bottom layer that keeps the `ef` best candidates. Neighbor selection uses the diversity heuristic from the HNSW
paper (prefer neighbors in different directions over near-duplicates), which is what keeps recall high.

**Filtered search:** HNSW doesn't know about memory status, so searches over-fetch candidates, drop the ones that don't
match (stale or superseded), widen the search if too few survive, and fall back to an exact scan if needed. In a test
with 2,000 memories where only 2% were active, filtered searches returned exactly the correct top 10 on 30/30 queries.
The catch: at that selectivity, the exact fallback runs most of the time, which is the known weak spot of
post-filtering on graph indexes.

### Crash safety

Every write goes to an append-only file (AOF) that is replayed on startup.

**`kill -9` crash test** (`crash_test.py`): a client writes as fast as it can, the server is killed at a random moment,
restarted, and every write the server acknowledged is checked.

| fsync policy | Crashes | Acknowledged writes | Lost |
|---|---|---|---|
| `everysec` | 5 | 757,295 | **0** |
| `always` | 3 | 282,595 | **0** |

Recovery took 0.6 seconds per restart, replaying up to ~185K writes.

**Torn writes:** a machine crash can cut the last write off halfway. The first version refused to start on such a file;
now replay stops at the last complete command, trims the partial one, and starts normally with all earlier data intact.

**Cost of each fsync policy** (SET throughput, 50 clients):

| Policy | Throughput | p50 latency |
|---|---|---|
| `always` (sync every write) | 42.9K/sec | 1.159 ms |
| `everysec` (sync once a second) | 113.9K/sec | 0.103 ms |
| `no` (let the OS decide) | 118.8K/sec | 0.103 ms |

`everysec` is 2.7× faster than `always` and within 4% of `no`, which is why it's the sensible default. On macOS, `fsync`
doesn't force the SSD's own cache to flush, so `always` looks cheaper here than it would on a Linux server.

### Full command list

- **Memory:**
  - `MEM.ADD`, `MEM.GET`, `MEM.SEARCH` (with `STATUS` filter), `MEM.STATUS`, `MEM.WHY`, `MEM.LIST`
  - `MEM.FILES`, `MEM.SEEN`, `MEM.CONFLICTS`, `MEM.RESOLVE`
- **Vectors:** `VADD`, `VSEARCH` (HNSW, tunable `EF`, `EXACT` mode)
- **Strings:** `GET`, `SET` (`EX` / `PX`), `INCR`, `STRLEN`, `TYPE`, `KEYS *`
- **Lists:** `RPUSH`, `LPUSH`, `LRANGE`, `LLEN`, `LPOP`, blocking `BLPOP`
- **Streams:** `XADD` (explicit, partial, and auto IDs), `XRANGE`, `XREAD` (with `BLOCK` and `$`)
- **Sorted sets:** `ZADD`, `ZRANK`, `ZRANGE`, `ZCARD`, `ZSCORE`, `ZREM`
- **Bitmaps:** `SETBIT`, `GETBIT`, `BITCOUNT`, `BITOP AND` / `OR` (binary-safe strings)
- **Geospatial:** `GEOADD`, `GEOPOS`, `GEODIST`, `GEOSEARCH ... FROMLONLAT ... BYRADIUS`
- **Transactions:** `MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH` (optimistic locking via per-key write versions)
- **Pub/Sub:** `SUBSCRIBE`, `UNSUBSCRIBE`, `PUBLISH`
- **Replication:**
  - Master–replica handshake (`PING`, `REPLCONF`, `PSYNC`) and command propagation
  - `REPLCONF GETACK` offset tracking and `WAIT`
- **Persistence:** RDB loading; AOF with a manifest, `appendfsync always` / `everysec` / `no`, and torn-write recovery
- **Auth:** `AUTH`, `ACL WHOAMI` / `GETUSER` / `SETUSER` (SHA-256 password hashes)

### Running the engine on its own

```sh
java src/main/java/Main.java                                   # port 6379
java src/main/java/Main.java --port 6380 --replicaof "localhost 6379"
java src/main/java/Main.java --dir /tmp/data --appendonly yes --appendfsync everysec
```

### Reproducing the benchmarks

Each benchmark runs against the engine you've started:

```sh
redis-benchmark -p 6379 -t set,get                 # throughput; also run against redis-server to compare
python vector_bench.py                             # HNSW recall and latency (start the engine with -Xmx4g)
python crash_test.py everysec 5                    # kill -9 crash test
python memory_mcp/calibrate.py                     # embedding similarity calibration
```

## Related work

Aether Cache is an early, open-source project. These are the production systems in the same space:

- **Vector search in Redis-style stores:**
  - [Redis 8 vector sets](https://redis.io/docs/latest/develop/data-types/vector-sets/) and
    [valkey-search](https://github.com/valkey-io/valkey-search) add HNSW vector search to Redis and Valkey.
  - [hnswlib](https://github.com/nmslib/hnswlib) is the reference HNSW implementation.
  - [Malkov & Yashunin (2016)](https://arxiv.org/abs/1603.09320) is the paper this index follows.
- **Memory for AI agents:** [Mem0](https://github.com/mem0ai/mem0), [Zep](https://www.getzep.com/),
  [Letta](https://github.com/letta-ai/letta), and the
  [MCP reference memory server](https://github.com/modelcontextprotocol/servers).
- **Instruction files:** [AGENTS.md](https://agents.md/) and `CLAUDE.md`. Aether Cache syncs into these rather than
  replacing them.

## Status and limitations

Aether Cache is v1 and early. It works end to end in automated tests and is now being tried in real Claude Code sessions.

**Not done yet:**
- **A with-memory vs. without-memory evaluation** on real coding tasks, to measure whether it actually reduces
  re-explaining.
- **AOF compaction:** the log only grows, and every status change adds a line.
- **Packaging as a prebuilt `.jar`,** so the engine starts instantly instead of compiling on launch.

**Engine limitations:**
- **Threading:** one OS thread per connection, not an event loop. That's fine for a local tool, but it won't scale to
  thousands of clients like Redis.
- **Transactions:** `EXEC` isn't isolated, so other clients' commands can interleave with a transaction's commands.
- **Vector index:** re-adding a vector updates it without rewiring its graph links, and there's no delete. Retired
  memories are filtered out, not removed.
- **Replication:** full resync only, and a new replica assumes the master's dataset is empty.
- **Smaller gaps:**
  - `KEYS *` lists only string keys.
  - `GEOSEARCH` scans every member.
  - ACL supports only the `default` user.
  - `HELLO` (RESP3) isn't supported, so clients must use RESP2.

## Project layout

```
src/main/java/Main.java      the engine (single file, from the CodeCrafters setup)
memory_mcp/server.py         MCP server: embeddings, git staleness, AGENTS.md/CLAUDE.md sync
memory_mcp/calibrate.py      similarity calibration for duplicate/contradiction thresholds
vector_bench.py              HNSW benchmark (writes results.json)
crash_test.py                kill -9 crash-safety test
```