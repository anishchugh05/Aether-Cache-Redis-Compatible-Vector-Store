[![progress-banner](https://backend.codecrafters.io/progress/redis/f43a7cd3-11e6-45dc-a8ba-9d6690c73ac0)](https://app.codecrafters.io/users/anishchugh05?r=2qF)

# Redis-compatible server in Java, with built-in vector search

A Redis-compatible in-memory data store written from scratch in Java. It speaks the Redis protocol (RESP), so
`redis-cli`, `redis-benchmark`, and client libraries like `redis-py` work with it unchanged.

On top of the core Redis features, it adds a native **HNSW vector index**, so an application can do key-value lookups
and semantic (meaning-based) search against the same store.

The core server was built by completing every stage and extension of the
[CodeCrafters "Build Your Own Redis"](https://codecrafters.io/challenges/redis) challenge. The vector search is my own
extension, built and benchmarked outside the challenge.

**Why this exists:** to learn how production data stores actually work (networking, replication, persistence,
indexing) by building one, and then measuring it honestly against the real thing.

## Results

### Core throughput vs. official Redis

`redis-benchmark -t set,get` (100K requests, 50 parallel clients, 3-byte values), same machine, same run conditions:

| | This server | Redis | This server as % of Redis |
|---|---|---|---|
| SET | 205.8K ops/sec | 233.6K ops/sec | 88% |
| GET | 225.7K ops/sec | 242.7K ops/sec | 93% |
| p50 latency | 0.119 ms | 0.111 ms | |
| p99 latency (SET / GET) | 0.375 / 0.231 ms | 0.359 / 0.159 ms | |
| max latency (SET) | 19.7 ms | 1.3 ms | JVM GC / JIT pauses |

Typical latency is on par with Redis. The main gap is worst-case latency, which comes from JVM garbage collection and
JIT warm-up (Redis is written in C and has neither).

### Vector search: HNSW vs. exact search

100,000 AG News headlines embedded with `all-MiniLM-L6-v2` (384 dimensions, cosine similarity), 500 held-out queries
from the AG News test split, k = 10. Recall is measured against exact (brute-force) search on the same data.

| Mode | Recall@10 | p50 latency | p99 latency |
|---|---|---|---|
| Exact (brute force) | 1.000 | 18.58 ms | 20.52 ms |
| HNSW, ef=10 | 0.905 | 0.49 ms | 1.38 ms |
| HNSW, ef=20 | 0.951 | 0.52 ms | 0.73 ms |
| HNSW, ef=50 | 0.985 | 0.66 ms | 0.89 ms |
| **HNSW, ef=100** | **0.994** | **0.84 ms** | **1.41 ms** |
| HNSW, ef=200 | 0.998 | 1.24 ms | 2.19 ms |

At ef=100, HNSW finds 99.4% of the true top-10 results about **22× faster** than exact search. Building the index for
all 100K vectors took 136 seconds (M=16, M0=32, efConstruction=200). Full setup and raw numbers are in `results.json`.

Latencies are measured from the Python client, so they include network and client overhead. Server-side search time is
lower.

### Test setup

- Machine: MacBook with Apple M4 (10 cores), 16 GB RAM, macOS 26.6
- Python 3.9.21, numpy 2.0.1, redis-py 5.0.1
- Redis (for comparison): 8.10.2
- Embeddings computed with `sentence-transformers` on the Mac's GPU (MPS)

Mode	   SET throughput	p50 latency
always	42.9K/sec	1.159 ms
everysec	113.9K/sec	0.103 ms
no	      118.8K/sec	0.103 ms

## Features

- **Protocol and networking:** RESP2 parser, concurrent clients (one thread per connection), pipelining
- **Strings:** `GET`, `SET` (with `EX` / `PX` expiry), `INCR`, `STRLEN`, `TYPE`, `KEYS *`
- **Lists:** `RPUSH`, `LPUSH`, `LRANGE`, `LLEN`, `LPOP`, blocking `BLPOP` (with timeouts, first-come-first-served)
- **Streams:** `XADD` (explicit, partial, and fully auto-generated IDs), `XRANGE`, `XREAD` (including `BLOCK` and `$`)
- **Sorted sets:** `ZADD`, `ZRANK`, `ZRANGE`, `ZCARD`, `ZSCORE`, `ZREM`
- **Bitmaps:** `SETBIT`, `GETBIT`, `BITCOUNT`, `BITOP AND` / `OR` (binary-safe strings)
- **Geospatial:** `GEOADD`, `GEOPOS`, `GEODIST`, `GEOSEARCH ... FROMLONLAT ... BYRADIUS` (Redis's 52-bit geohash scores)
- **Transactions:** `MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH` (optimistic locking via per-key write versions)
- **Pub/Sub:** `SUBSCRIBE`, `UNSUBSCRIBE`, `PUBLISH`, subscribed mode
- **Replication:** full master–replica handshake (`PING`, `REPLCONF`, `PSYNC`), command propagation, `REPLCONF GETACK`
  offset tracking, `WAIT`
- **Persistence:** loads RDB snapshots on startup; append-only file (AOF) logging with a manifest, `appendfsync always`,
  and replay on restart
- **Auth:** `AUTH`, `ACL WHOAMI`, `ACL GETUSER`, `ACL SETUSER` (SHA-256 password hashes), per-connection authentication
- **Vector search (my extension):** `VADD`, `VSEARCH` with an HNSW index, a tunable `EF`, and an `EXACT` mode

## Vector search usage

```
VADD <key> <id> <x1> <x2> ... <xn>
VSEARCH <key> <k> [EF <n>] [EXACT] <x1> <x2> ... <xn>
```

- The first vector added to a key fixes its dimension count.
- Vectors are normalized when stored, so similarity is cosine similarity.
- `VSEARCH` returns `[id, score, id, score, ...]`, most similar first.
- `EF` sets how wide the HNSW search is (default 100). Higher is more accurate and slower.
- `EXACT` skips the index and compares against every vector.

```
> VADD docs cat 1 0 0
(integer) 1
> VADD docs dog 0.9 0.1 0
(integer) 1
> VADD docs car 0 0 1
(integer) 1
> VSEARCH docs 2 1 0 0
1) "cat"
2) "1.000000"
3) "dog"
4) "0.993884"
```

### How the HNSW index works

Each vector is linked to its nearest neighbors in a graph. A few randomly chosen nodes also live on higher layers with
longer-range links. A search starts at the top layer, greedily hops toward the query, drops down layer by layer, and
finishes with a wider best-first search on the bottom layer that keeps the `ef` best candidates. Neighbor selection uses
the diversity heuristic from the HNSW paper, which prefers neighbors in different directions over near-duplicates.
That heuristic is what keeps recall high.

## Running it

Requires Java 11+.

```sh
# start the server (default port 6379)
java src/main/java/Main.java

# some options
java src/main/java/Main.java --port 6380
java src/main/java/Main.java --port 6380 --replicaof "localhost 6379"
java src/main/java/Main.java --dir /tmp/redis-data --dbfilename dump.rdb
java src/main/java/Main.java --dir /tmp/redis-data --appendonly yes --appendfsync always
```

Then connect with any Redis client, for example `redis-cli -p 6379`.

### Reproducing the benchmarks

Core throughput (run once against this server, once against `redis-server` on the same machine):

```sh
redis-benchmark -p 6379 -t set,get
```

Vector search:

```sh
pip install sentence-transformers datasets redis numpy
java -Xmx4g src/main/java/Main.java      # in one terminal
python vector_bench.py                   # in another
```

The first run downloads AG News and the embedding model and caches the embeddings to `embeddings_cache.npz`.

## Related work

This project is for learning. Production-grade versions of the same ideas already exist:

- **[Redis 8 vector sets](https://redis.io/docs/latest/develop/data-types/vector-sets/):** Redis itself added an
  HNSW-based vector type (`VADD`, `VSIM`) in Redis 8.
- **[valkey-search](https://github.com/valkey-io/valkey-search):** vector similarity search as a module for Valkey,
  the open-source Redis fork.
- **[hnswlib](https://github.com/nmslib/hnswlib):** the reference C++ HNSW implementation, widely used as a baseline.
- **[Malkov & Yashunin, "Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small
  World graphs"](https://arxiv.org/abs/1603.09320):** the HNSW paper this index is based on.

## Limitations

Known gaps and simplifications:

- **Threading:** one OS thread per connection instead of an event loop. Fine for tens to hundreds of clients, but it
  won't scale to thousands the way Redis does.
- **Transactions:** `EXEC` runs the queued commands in order, but other clients' commands can interleave with them.
- **Vectors aren't persisted:** they're lost on restart (`VADD` isn't written to the AOF or RDB).
- **Vector updates and deletes:** re-adding an existing id updates its vector without rewiring its graph links, and
  there's no delete.
- **Persistence:** `appendfsync everysec` isn't implemented yet (writes are synced only with `always`), there's no AOF
  rewrite or compaction, and RDB is load-only (no `SAVE`) for string values.
- **Replication:** full resync only (no partial resync), and a new replica assumes the master's dataset is empty.
- **Smaller gaps:**
  - `KEYS *` only lists string keys.
  - `GEOSEARCH` checks every member instead of using geohash cells.
  - ACL supports only the `default` user.
  - `HELLO` (RESP3) and `CLIENT` aren't supported, so `redis-py` needs `protocol=2`.

## Project layout

Everything lives in `src/main/java/Main.java` (a single file, per the CodeCrafters setup). The benchmark script is
`vector_bench.py`.