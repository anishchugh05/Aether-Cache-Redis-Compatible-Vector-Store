"""
memstore MCP server: a shared, local memory for AI coding tools (Claude Code, Cursor, ...).

AI tools call these tools over MCP; this server turns text into embeddings and stores/searches them
in the Java memory store (Main.java) over the Redis protocol.

Settings (environment variables, all optional):
  MEMORY_HOST / MEMORY_PORT  where the Java store runs (default localhost:6379)
  MEMORY_SOURCE              which tool this is, e.g. "claude-code" or "cursor" (recorded with each memory)
  MEMORY_PROJECT             project name (default: the git repo's folder name, or the current folder's)
  MEMORY_MODEL               embedding model (default sentence-transformers/all-MiniLM-L6-v2, 384 dims)
  MEMORY_EMBEDDER=fake       testing only: a simple word-hashing embedder that needs no download
"""
import hashlib
import math
import os
import subprocess
import uuid
import warnings
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import redis

try:
    from mcp.server.mcpserver import MCPServer  # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x

HOST = os.environ.get("MEMORY_HOST", "localhost")
PORT = int(os.environ.get("MEMORY_PORT", "6379"))
SOURCE = os.environ.get("MEMORY_SOURCE", "unknown")
MODEL = os.environ.get("MEMORY_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
# Existing memories at least this similar to a newly saved one are shown to the AI to judge.
# Low on purpose: in calibration, "we use Postgres" vs "we migrated to MySQL" scored only 0.30.
RELATED_SIMILARITY = float(os.environ.get("MEMORY_RELATED_SIMILARITY", "0.25"))
SESSION = uuid.uuid4().hex[:8]  # each AI tool session starts its own copy of this server

mcp = MCPServer(
    "memstore",
    instructions=(
        "Shared memory for this project, kept across sessions and across AI tools. "
        "At the start of a task, call recall with a short description of the task. "
        "When you learn something durable that a future session would need (a convention, a setup gotcha, "
        "a decision and its reason), call remember. If a memory turns out to be wrong, call mark_outdated."
    ),
)
_db = None
_embedder = None


# ---------- helpers ----------

def project_name() -> str:
    """The project memories belong to: MEMORY_PROJECT, else the git repo's folder name, else this folder's."""
    if os.environ.get("MEMORY_PROJECT"):
        return os.environ["MEMORY_PROJECT"]
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip()).name
    except Exception:
        pass
    return Path.cwd().name


def db() -> redis.Redis:
    global _db
    if _db is None:
        # protocol=2 and no lib_name/lib_version: the Java store speaks RESP2 and doesn't implement
        # HELLO or CLIENT SETINFO, which newer redis-py clients send by default
        with warnings.catch_warnings():  # newer redis-py warns that lib_name is deprecated; harmless here
            warnings.simplefilter("ignore")
            _db = redis.Redis(host=HOST, port=PORT, protocol=2, decode_responses=True,
                              lib_name=None, lib_version=None)
    return _db


def fake_embed(text: str, dim: int = 384) -> List[float]:
    """Testing only: hashes each word into a slot, so texts sharing words come out similar."""
    v = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.md5(word.strip(".,!?\"'").encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 64) % 2 else -1.0
    return v


def embed(text: str) -> List[str]:
    """Text -> embedding, formatted as strings ready to send. Loads the model on first use."""
    global _embedder
    if os.environ.get("MEMORY_EMBEDDER") == "fake":
        vec = fake_embed(text)
    else:
        if _embedder is None:
            from fastembed import TextEmbedding  # imported lazily so the server starts instantly
            _embedder = TextEmbedding(MODEL)
        vec = next(iter(_embedder.embed([text]))).tolist()
    return [f"{x:.6f}" for x in vec]


def store_down() -> str:
    return (f"The memory store isn't running at {HOST}:{PORT}. Start it with: "
            f"java src/main/java/Main.java --port {PORT} --dir ~/.memstore --appendonly yes")


def when(ms: str) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


# ---------- tools the AI can call ----------

@mcp.tool()
def remember(fact: str, files: Optional[List[str]] = None) -> str:
    """Save a durable fact about this project so future sessions, in any AI tool, know it.

    Good facts: conventions ("we use pnpm, not npm"), setup gotchas ("tests need DOCKER_HOST set"),
    decisions and their reasons. Not: temporary task details or anything in the code already.
    files: repo-relative paths this fact depends on (e.g. ["docker-compose.yml"]), so the memory
    can be flagged when those files change.
    """
    args = ["MEM.ADD", project_name(), fact, "SOURCE", SOURCE, "SESSION", SESSION]
    if files:
        args += ["FILES", ",".join(f.strip() for f in files if f.strip())]
    vector = embed(fact)
    try:
        mem_id, outcome, related = db().execute_command(*args, "VECTOR", *vector)
        if outcome == "duplicate":
            return f"Already known as {mem_id}; noted that it came up again. Nothing new was stored."
        reply = f"Saved as {mem_id}."
        if outcome == "possible_conflict":
            old = dict(zip(*[iter(db().execute_command("MEM.GET", related))] * 2))
            reply += (f" It is very similar to {related}: \"{old.get('text')}\" and may contradict it. "
                      f"If unsure, ask the user, then call resolve_conflict.")
        # Similarity can't tell "contradicts" from "related" (a real contradiction can score low), so show
        # the closest existing memories and let the AI judge whether the new fact replaces any of them.
        hits = db().execute_command("MEM.SEARCH", project_name(), 4, "VECTOR", *vector)
        others = [(mid, float(sc), text) for mid, sc, text in hits
                  if mid not in (mem_id, related) and float(sc) >= RELATED_SIMILARITY]
        if others:
            reply += "\nRelated existing memories:\n" + "\n".join(
                f"[{mid}] (similarity {sc:.2f}) {text}" for mid, sc, text in others)
            reply += (f"\nIf {mem_id} makes any of these no longer true, call replace_memory(old_id, \"{mem_id}\"). "
                      f"Otherwise do nothing.")
        return reply
    except redis.ConnectionError:
        return store_down()


@mcp.tool()
def recall(query: str, limit: int = 5) -> str:
    """Search this project's memories by meaning. Use it at the start of a task, or whenever
    project-specific knowledge (setup, conventions, past decisions) might matter.
    Only current (active) memories are returned; outdated or replaced ones are skipped."""
    try:
        hits = db().execute_command("MEM.SEARCH", project_name(), max(1, min(limit, 20)), "VECTOR", *embed(query))
    except redis.ConnectionError:
        return store_down()
    if not hits:
        return "No memories found for this project yet."
    return "\n".join(f"[{mid}] (similarity {float(score):.2f}) {text}" for mid, score, text in hits)


@mcp.tool()
def explain_memory(memory_id: str) -> str:
    """Show where a memory came from (which tool and session saved it, which files it's linked to)
    and everything that has happened to it since. Use it when a memory looks wrong or surprising."""
    try:
        r = db().execute_command("MEM.WHY", memory_id)
    except redis.ConnectionError:
        return store_down()
    if r is None:
        return f"No memory with id {memory_id}."
    d = dict(zip(r[0::2], r[1::2]))
    lines = [f"{d['id']}: \"{d['text']}\"",
             f"status: {d['status']}, project: {d['project']}",
             f"saved by: {d['source']}" + (f" (session {d['session']})" if d["session"] else ""),
             f"linked files: {d['files'] or 'none'}",
             "history:"]
    lines += [f"  {when(t)}  {event}" + (f": {details}" if details else "") for t, event, details in d["history"]]
    return "\n".join(lines)


@mcp.tool()
def mark_outdated(memory_id: str, reason: str) -> str:
    """Mark a memory as no longer true, with a short reason. It stops showing up in recall
    but keeps its history, so you can still see what it said and why it was retired."""
    try:
        reply = db().execute_command("MEM.STATUS", memory_id, "stale", "REASON", reason)
    except redis.ConnectionError:
        return store_down()
    except redis.ResponseError as e:
        return f"Couldn't update {memory_id}: {e}"
    return f"Marked {memory_id} as outdated." if reply == "OK" else str(reply)


@mcp.tool()
def replace_memory(old_id: str, new_id: str) -> str:
    """Retire an older memory because a newer one replaces it (e.g. "we use Postgres" replaced by
    "we migrated to MySQL"). The old memory stops showing up in recall but keeps its history."""
    try:
        new = db().execute_command("MEM.GET", new_id)
        if new is None:
            return f"No memory with id {new_id}."
        new_text = dict(zip(new[0::2], new[1::2]))["text"]
        db().execute_command("MEM.STATUS", old_id, "superseded", "REASON", f"replaced by {new_id}: \"{new_text}\"")
    except redis.ConnectionError:
        return store_down()
    except redis.ResponseError as e:
        return f"Couldn't replace: {e}"
    return f"{old_id} retired; {new_id} replaces it."


@mcp.tool()
def list_conflicts() -> str:
    """List pairs of memories in this project that may contradict each other and need a decision."""
    try:
        rows = db().execute_command("MEM.CONFLICTS", project_name())
    except redis.ConnectionError:
        return store_down()
    if not rows:
        return "No conflicts waiting for review."
    return "\n".join(f"{new_id} \"{new_text}\" may contradict {old_id} \"{old_text}\" (similarity {sim})"
                     for new_id, new_text, old_id, old_text, sim in rows)


@mcp.tool()
def resolve_conflict(new_id: str, old_id: str, replaces_old: bool) -> str:
    """Settle a possible conflict. replaces_old=True: the new memory replaces the old one (the old one
    is retired). replaces_old=False: both are true, keep both. Ask the user if you're not sure."""
    try:
        db().execute_command("MEM.RESOLVE", new_id, old_id, "SUPERSEDE" if replaces_old else "KEEP")
    except redis.ConnectionError:
        return store_down()
    except redis.ResponseError as e:
        return f"Couldn't resolve: {e}"
    return f"{old_id} retired; {new_id} replaces it." if replaces_old else f"Kept both {new_id} and {old_id}."


if __name__ == "__main__":
    mcp.run()  # talks to the AI tool over stdin/stdout
