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


# ---------- git-aware staleness ----------

def repo_root() -> Path:
    """The git repo's top folder (file paths in memories are relative to it), else the current folder."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip())
    except Exception:
        pass
    return Path.cwd()


def relative(path: str) -> str:
    """Makes a path repo-relative, so memories still match if the repo is opened from a subfolder."""
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(repo_root().resolve()))
        except ValueError:
            return str(p)
    return path.strip()


def content_hashes(paths: List[str]) -> dict:
    """path -> git hash of the file's current contents (what git would store), or "missing"."""
    root = repo_root()
    result = {p: "missing" for p in paths}
    present = [p for p in paths if (root / p).is_file()]
    if present:
        out = subprocess.run(["git", "hash-object", "--"] + present, cwd=root,
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            for p, h in zip(present, out.stdout.split()):
                result[p] = h
    return result


def link_files(paths: List[str]) -> str:
    """["a.py", "b.yml"] -> "a.py@<hash>,b.yml@<hash>": the files plus a snapshot of their contents."""
    rel = [relative(p) for p in paths if p.strip()]
    hashes = content_hashes(rel)
    return ",".join(f"{p}@{hashes[p]}" for p in rel)


def check_staleness() -> List[str]:
    """Marks active memories stale when a file they depend on has changed since they were saved.
    Returns one line per memory that just went stale."""
    newly_stale = []
    linked = []  # (id, text, [(path, saved hash)])
    for mid, _status, files, text in db().execute_command("MEM.LIST", project_name()):
        entries = [f.rsplit("@", 1) for f in files.split(",") if "@" in f]
        if entries:
            linked.append((mid, text, entries))
    if not linked:
        return newly_stale
    current = content_hashes(sorted({path for _, _, entries in linked for path, _ in entries}))
    for mid, text, entries in linked:
        changed = [path for path, saved in entries if current.get(path) != saved]
        if changed:
            what = ", ".join(f"{p} {'was deleted' if current.get(p) == 'missing' else 'changed'}" for p in changed)
            db().execute_command("MEM.STATUS", mid, "stale", "REASON", f"{what} since this was saved")
            newly_stale.append(f"[{mid}] \"{text}\" is now marked outdated: {what}.")
    return newly_stale


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
        args += ["FILES", link_files(files)]
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
        stale = check_staleness()  # so we never hand back a memory whose files changed underneath it
        hits = db().execute_command("MEM.SEARCH", project_name(), max(1, min(limit, 20)), "VECTOR", *embed(query))
    except redis.ConnectionError:
        return store_down()
    lines = [f"[{mid}] (similarity {float(score):.2f}) {text}" for mid, score, text in hits]
    if not lines:
        lines = ["No memories found for this project yet."]
    if stale:
        lines += ["", "These memories were just retired because files they depend on changed. If one is still "
                      "true, call confirm_memory; if it's wrong, save the corrected fact with remember:"] + stale
    return "\n".join(lines)


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
def check_stale() -> str:
    """Check whether any memory's linked files have changed since it was saved, and retire those
    memories. recall already does this automatically; call it directly after big changes."""
    try:
        stale = check_staleness()
    except redis.ConnectionError:
        return store_down()
    return "\n".join(stale) if stale else "All memories linked to files are still up to date."


@mcp.tool()
def confirm_memory(memory_id: str) -> str:
    """Mark a memory as still true (e.g. after it was retired because a linked file changed, but the
    fact itself didn't). Re-links it to the current version of its files."""
    try:
        r = db().execute_command("MEM.GET", memory_id)
        if r is None:
            return f"No memory with id {memory_id}."
        files = [f.rsplit("@", 1)[0] for f in dict(zip(r[0::2], r[1::2]))["files"].split(",") if f]
        if files:
            db().execute_command("MEM.FILES", memory_id, link_files(files), "REASON", "confirmed still true")
        db().execute_command("MEM.STATUS", memory_id, "active", "REASON", "confirmed still true")
    except redis.ConnectionError:
        return store_down()
    return f"Confirmed {memory_id}; it's active again" + (" and linked to the current file versions." if files else ".")


# ---------- syncing to AGENTS.md / CLAUDE.md ----------

SYNC_START = "<!-- memstore:start -->"
SYNC_END = "<!-- memstore:end -->"
SYNC_FILES = ["AGENTS.md", "CLAUDE.md"]


def memory_block() -> str:
    rows = db().execute_command("MEM.LIST", project_name())
    lines = [SYNC_START,
             "## Project memory (managed by memstore)",
             "<!-- Generated from memstore; edits inside this section are overwritten on the next sync. -->",
             ""]
    lines += [f"- {text} ({mid})" for mid, _status, _files, text in rows] or ["_No memories yet._"]
    lines.append(SYNC_END)
    return "\n".join(lines)


def sync_files() -> str:
    """Writes the project's active memories into the managed section of AGENTS.md / CLAUDE.md.
    Only the section between the markers is touched; files that don't exist aren't created, unless
    neither exists, in which case AGENTS.md is."""
    root = repo_root()
    block = memory_block()
    targets = [root / name for name in SYNC_FILES if (root / name).exists()] or [root / "AGENTS.md"]
    report = []
    for path in targets:
        old = path.read_text() if path.exists() else ""
        if SYNC_START in old and SYNC_END in old:
            before = old[:old.index(SYNC_START)]
            after = old[old.index(SYNC_END) + len(SYNC_END):]
            new = before + block + after
        else:
            new = (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"
        if new == old:
            report.append(f"{path.name}: already up to date")
            continue
        tmp = path.with_name(path.name + ".memstore-tmp")
        tmp.write_text(new)
        os.replace(tmp, path)  # swap in the new version all at once, so a crash can't leave half a file
        report.append(f"{path.name}: {'updated' if old else 'created'}")
    return "\n".join(report)


@mcp.tool()
def sync_to_files() -> str:
    """Write this project's current memories into the managed section of AGENTS.md / CLAUDE.md, so they're
    committed with the code: teammates and tools without memstore see them, and changes show up in reviews.
    Call it when the user asks, or after a batch of new memories."""
    try:
        check_staleness()  # never write out a memory whose files have changed
        return sync_files()
    except redis.ConnectionError:
        return store_down()


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
    import sys
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "sync":     # python server.py sync   -> update AGENTS.md / CLAUDE.md in this repo
        print(sync_to_files())
    elif command == "check":  # python server.py check  -> retire memories whose files changed
        print(check_stale())
    else:
        mcp.run()  # talks to the AI tool over stdin/stdout
