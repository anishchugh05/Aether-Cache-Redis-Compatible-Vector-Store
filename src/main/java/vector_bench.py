"""
Benchmark the server's HNSW vector search on real text embeddings.

1. Embeds 100K AG News headlines with all-MiniLM-L6-v2 (384 dims), cached to disk after the first run
2. Loads them into the server with VADD
3. Runs held-out queries with EXACT (ground truth) and HNSW at several EF values
4. Prints recall@10 and latency for each
"""
import json, os, platform, subprocess, sys, time
from datetime import datetime
import numpy as np
import redis

N = 100_000          # vectors to index
NUM_QUERIES = 500    # held-out queries to test with
K = 10
EF_VALUES = [10, 20, 50, 100, 200]
CACHE = "embeddings_cache.npz"
RESULTS_FILE = "results.json"
KEY = "news"
DATASET = "fancyzhx/ag_news (train = documents, test = queries)"
MODEL = "all-MiniLM-L6-v2"
# The server's HNSW build settings (constants in VectorSet in Main.java), recorded with the results
HNSW_SETTINGS = {"M": 16, "M0": 32, "efConstruction": 200}

def run_cmd(args):
    """Runs a shell command and returns its output, or None if it isn't available."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        return (out.stdout or out.stderr).strip() or None  # some tools (like java -version) print to stderr
    except Exception:
        return None

def environment():
    """Describes the machine and software, so the numbers can be reproduced."""
    env = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "redis_py": redis.__version__,
    }
    # CPU and RAM: sysctl on macOS, /proc on Linux
    cpu = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
    mem = run_cmd(["sysctl", "-n", "hw.memsize"])
    if cpu is None and os.path.exists("/proc/cpuinfo"):
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    if mem is None and os.path.exists("/proc/meminfo"):
        mem = str(int(open("/proc/meminfo").readline().split()[1]) * 1024)
    env["cpu"] = cpu
    env["cpu_cores"] = os.cpu_count()
    env["ram_gb"] = round(int(mem) / 1024**3, 1) if mem and mem.isdigit() else None
    java = run_cmd(["java", "-version"])  # java prints its version to stderr
    env["java"] = java.splitlines()[0] if java else None
    return env

def load_embeddings():
    if os.path.exists(CACHE):
        print(f"Loading cached embeddings from {CACHE}")
        data = np.load(CACHE)
        return data["docs"], data["queries"]

    from datasets import load_dataset
    from sentence_transformers import SentenceTransformer
    import torch

    print("Downloading AG News...")
    train = load_dataset("fancyzhx/ag_news", split="train")   # 120K news items -> our documents
    test = load_dataset("fancyzhx/ag_news", split="test")     # separate items -> our queries
    docs_text = train["text"][:N]
    query_text = test["text"][:NUM_QUERIES]

    device = "mps" if torch.backends.mps.is_available() else "cpu"  # Apple GPU if available
    print(f"Embedding {len(docs_text)} documents on {device} (takes a few minutes the first time)...")
    model = SentenceTransformer("all-MiniLM-L6-v2", device=device)
    docs = model.encode(docs_text, batch_size=256, show_progress_bar=True, convert_to_numpy=True)
    queries = model.encode(query_text, batch_size=256, convert_to_numpy=True)
    np.savez(CACHE, docs=docs.astype(np.float32), queries=queries.astype(np.float32))
    return docs, queries

def vec_args(v):
    return [f"{x:.6f}" for x in v]

def main():
    env = environment()
    print("Environment:")
    for k, v in env.items():
        print(f"  {k}: {v}")

    docs, queries = load_embeddings()
    print(f"{len(docs)} documents, {len(queries)} queries, {docs.shape[1]} dims")

    # protocol=2 keeps redis-py on RESP2 (no HELLO), and lib_name/lib_version=None skips CLIENT SETINFO;
    # our server doesn't implement those two connection-setup commands
    r = redis.Redis(host="localhost", port=6379, protocol=2, lib_name=None, lib_version=None)

    print("Loading vectors into the server...")
    t = time.time()
    batch = 500
    for start in range(0, len(docs), batch):
        pipe = r.pipeline(transaction=False)
        for i in range(start, min(start + batch, len(docs))):
            pipe.execute_command("VADD", KEY, f"doc{i}", *vec_args(docs[i]))
        pipe.execute()
        done = min(start + batch, len(docs))
        if done % 10000 == 0:
            print(f"  {done}/{len(docs)}  ({time.time() - t:.0f}s)")
    index_seconds = time.time() - t
    print(f"Indexed {len(docs)} vectors in {index_seconds:.1f}s")

    def run(extra):
        ids, times = [], []
        for q in queries:
            t0 = time.perf_counter()
            res = r.execute_command("VSEARCH", KEY, K, *extra, *vec_args(q))
            times.append((time.perf_counter() - t0) * 1000)
            ids.append({res[i].decode() for i in range(0, len(res), 2)})
        return ids, np.array(times)

    rows = []
    truth, exact_times = run(["EXACT"])
    rows.append({"mode": "EXACT", "recall_at_10": 1.0,
                 "p50_ms": round(float(np.percentile(exact_times, 50)), 3),
                 "p99_ms": round(float(np.percentile(exact_times, 99)), 3)})
    for ef in EF_VALUES:
        found, times = run(["EF", ef])
        recall = float(np.mean([len(f & t) / K for f, t in zip(found, truth)]))
        rows.append({"mode": f"HNSW ef={ef}", "ef": ef, "recall_at_10": round(recall, 4),
                     "p50_ms": round(float(np.percentile(times, 50)), 3),
                     "p99_ms": round(float(np.percentile(times, 99)), 3)})

    print(f"\n{'mode':<12}{'recall@10':>10}{'p50 ms':>9}{'p99 ms':>9}")
    for row in rows:
        print(f"{row['mode']:<12}{row['recall_at_10']:>10.3f}{row['p50_ms']:>9.2f}{row['p99_ms']:>9.2f}")

    # Save everything needed to understand and reproduce this run
    report = {
        "environment": env,
        "settings": {
            "dataset": DATASET,
            "embedding_model": MODEL,
            "documents": int(len(docs)),
            "queries": int(len(queries)),
            "dimensions": int(docs.shape[1]),
            "k": K,
            "similarity": "cosine",
            "hnsw": HNSW_SETTINGS,
            "latency_measured": "client-side (redis-py, includes network and client overhead)",
        },
        "index_build_seconds": round(index_seconds, 1),
        "results": rows,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved results and setup to {RESULTS_FILE}")

if __name__ == "__main__":
    main()