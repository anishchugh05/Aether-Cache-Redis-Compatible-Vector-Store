"""
The MCP server: the tools AI coding tools (Claude Code, Cursor, Codex, ...) call to use Mnemolith.

It turns text into embeddings, links memories to git file versions, and stores and searches them in the
Java engine over the Redis protocol. The engine is started automatically if it isn't running.
Settings are environment variables; see config.py.
"""
import hashlib
import os
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import redis

from . import __version__, config, engine, legacy

try:
    from mcp.server.mcpserver import MCPServer  # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x

SESSION = uuid.uuid4().hex[:8]  # each AI tool session starts its own copy of this server

# Shown to the AI by every MCP client, and also written into the tools' own instruction files by
# `mnemolith connect`, because AI tools otherwise default to their built-in memory.
INSTRUCTIONS = (
    "Mnemolith is this project's shared memory, kept across sessions and across AI tools (Claude Code, "
    "Cursor, Codex), and checked against the code so it never goes stale silently. For project knowledge, use "
    "its tools instead of any built-in memory, notes file, or memory directory:\n"
    "- At the start of every task, call recall with a short description of the task.\n"
    "- When the user asks you to remember, save, or note something about the project, or you learn something "
    "durable that a future session would need (a convention, a setup gotcha, a decision and its reason), call "
    "remember. Pass files= with the files the fact depends on, so it's retired automatically when they change.\n"
    "- If a memory turns out to be wrong, call mark_outdated. If a new fact makes an old one untrue, call "
    "replace_memory."
)

try:
    mcp = MCPServer("mnemolith", instructions=INSTRUCTIONS, version=__version__)
except Exception:  # mcp 1.x has no version argument; set it on the underlying server instead
    mcp = MCPServer("mnemolith", instructions=INSTRUCTIONS)
    try:
        mcp._mcp_server.version = __version__
    except AttributeError:
        pass

def quiet_libraries() -> None:
    """Hides the download libraries' request logs, warnings and progress bars, which otherwise flood the
    terminal (and spill out after setup says it's done). Our own messages say what's happening."""
    import logging
    import warnings
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    warnings.filterwarnings("ignore", message="Cannot enable progress bars")  # fastembed asks; we said no
    # Windows without Developer Mode can't make symlinks; the model cache still works, it just warns loudly
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    warnings.filterwarnings("ignore", message=".*symlinks.*")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    for name in ("httpx", "httpcore", "huggingface_hub", "filelock", "urllib3"):
        logging.getLogger(name).setLevel(logging.ERROR)
    try:
        from loguru import logger  # fastembed logs its retries through loguru
        logger.disable("fastembed")
    except ImportError:
        pass


quiet_libraries()

_db = None
_embedder = None
_embedder_lock = threading.Lock()


class EngineDown(Exception):
    pass


# ---------- engine and embeddings ----------

def db() -> redis.Redis:
    global _db
    if _db is None:
        _db = engine.client()
        try:
            legacy.migrate_keys(_db)  # one-time rename of what Aether Cache stored, if the engine is up
        except Exception:
            pass
    return _db


def run(*args):
    """Sends one command to the engine. If the engine isn't running, starts it and tries once more."""
    try:
        return db().execute_command(*args)
    except (redis.ConnectionError, redis.TimeoutError):
        try:
            engine.ensure_running()
        except engine.EngineError as e:
            raise EngineDown(f"its engine isn't running and couldn't be started. {e}")
        return db().execute_command(*args)


def store_down(error: Exception) -> str:
    return (f"Mnemolith hit a problem: {error}\nRun `mnemolith doctor` in a terminal for help, "
            f"or tell us at {config.CONTACT_URL}")


def fake_embed(text: str, dim: int = 384) -> List[float]:
    """Testing only: hashes each word into a slot, so texts sharing words come out similar."""
    v = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.md5(word.strip(".,!?\"'").encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 64) % 2 else -1.0
    return v


def embedder():
    """The embedding model, loaded (and downloaded, the first time) on first use."""
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            from fastembed import TextEmbedding  # imported lazily so the server starts instantly
            config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
            # a fixed cache folder: fastembed's default is a temp folder, which the OS may wipe on reboot
            _embedder = TextEmbedding(config.MODEL, cache_dir=str(config.MODELS_DIR))
    return _embedder


def embed(text: str) -> List[str]:
    """Text -> embedding, formatted as strings ready to send."""
    if config.FAKE_EMBEDDER:
        vec = fake_embed(text)
    else:
        try:
            vec = next(iter(embedder().embed([text]))).tolist()
        except Exception as e:
            raise EngineDown(f"the embedding model couldn't be loaded ({e}). The first run downloads it from "
                             f"Hugging Face (huggingface.co, about 90 MB), so check that your network allows it.")
    return [f"{x:.6f}" for x in vec]


def embed_many(texts: List[str]) -> List[List[str]]:
    """Like embed, for many texts at once (the model is much faster in batches)."""
    if config.FAKE_EMBEDDER:
        vecs = [fake_embed(t) for t in texts]
    else:
        try:
            vecs = [v.tolist() for v in embedder().embed(texts)]
        except Exception as e:
            raise EngineDown(f"the embedding model couldn't be loaded ({e}). The first run downloads it from "
                             f"Hugging Face (huggingface.co, about 90 MB), so check that your network allows it.")
    return [[f"{x:.6f}" for x in v] for v in vecs]


def warm_up() -> None:
    """Starts the engine and loads the model in the background, so the AI's first call is fast."""
    def work():
        try:
            engine.ensure_running()
        except Exception:
            pass  # reported properly when a tool is actually called
        try:
            if not config.FAKE_EMBEDDER:
                embedder()
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


def when(ms: str) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


# ---------- the project and its files ----------

def workdir() -> Path:
    override = config.repo_override()
    if override and Path(override).expanduser().is_dir():
        return Path(override).expanduser()
    return Path.cwd()


def git_toplevel() -> Optional[Path]:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=workdir(),
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None  # git not installed, or too slow
    if top.returncode == 0 and top.stdout.strip():
        return Path(top.stdout.strip())
    return None


REPO_KEY = "mnemolith:repo:"  # mnemolith:repo:<project> -> the repo's folder, so the dashboard can check its files
_registered = set()


def project_name() -> str:
    """The project memories belong to: MNEMOLITH_PROJECT, else the git repo's folder name, else this folder's."""
    name = config.project_override() or (git_toplevel() or workdir()).name
    _remember_repo_location(name)
    return name


def _remember_repo_location(project: str) -> None:
    """Records where this project's repo lives (once per session), so `mnemolith ui`, which runs
    from anywhere, can still check its memories' files. Best effort: never gets in the AI's way."""
    top = git_toplevel()
    if top is None or (project, str(top)) in _registered:
        return
    _registered.add((project, str(top)))
    try:
        db().set(REPO_KEY + project, str(top))
    except Exception:
        _registered.discard((project, str(top)))


def repo_root() -> Path:
    """File paths in memories are relative to this: the git repo's top folder, else the current folder."""
    return git_toplevel() or workdir()


def relative(path: str, root: Optional[Path] = None) -> str:
    """Makes a path repo-relative, so memories still match if the repo is opened from a subfolder."""
    p = Path(path.strip())
    if p.is_absolute():
        try:
            return p.resolve().relative_to((root or repo_root()).resolve()).as_posix()
        except ValueError:
            return str(p)
    return p.as_posix()


def blob_hash(path: Path) -> str:
    """The same hash `git hash-object` gives for a file's contents, computed directly, so it works without git."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def content_hashes(paths: List[str], root: Optional[Path] = None) -> dict:
    """path -> hash of the file's current contents, or "missing"."""
    root = root or repo_root()
    result = {}
    for p in paths:
        try:
            result[p] = blob_hash(root / p) if (root / p).is_file() else "missing"
        except OSError:
            result[p] = "missing"
    return result


def link_files(paths: List[str], root: Optional[Path] = None) -> str:
    """["a.py", "b.yml"] -> "a.py@<hash>,b.yml@<hash>": the files plus a snapshot of their contents."""
    root = root or repo_root()
    rel = [relative(p, root) for p in paths if p.strip()]
    hashes = content_hashes(rel, root)
    return ",".join(f"{p}@{hashes[p]}" for p in rel)


def check_staleness(project: Optional[str] = None, root: Optional[Path] = None) -> List[str]:
    """Marks active memories stale when a file they depend on has changed since they were saved.
    Returns one line per memory that just went stale. Defaults to the current project and repo."""
    project = project or project_name()
    root = root or repo_root()
    newly_stale = []
    linked = []  # (id, text, [(path, saved hash)])
    for mid, _status, files, text in run("MEM.LIST", project):
        entries = [f.rsplit("@", 1) for f in files.split(",") if "@" in f]
        if entries:
            linked.append((mid, text, entries))
    if not linked:
        return newly_stale
    current = content_hashes(sorted({path for _, _, entries in linked for path, _ in entries}), root)
    for mid, text, entries in linked:
        changed = [path for path, saved in entries if current.get(path) != saved]
        if changed:
            what = ", ".join(f"{p} {'was deleted' if current.get(p) == 'missing' else 'changed'}" for p in changed)
            run("MEM.STATUS", mid, "stale", "REASON", f"{what} since this was saved")
            newly_stale.append(f"[{mid}] \"{text}\" is now marked outdated: {what}.")
    return newly_stale


def confirm(memory_id: str, root: Optional[Path] = None) -> Optional[bool]:
    """Marks a memory still true and re-links it to the current versions of its files.
    Returns whether it has linked files, or None if there's no such memory."""
    r = run("MEM.GET", memory_id)
    if r is None:
        return None
    files = [f.rsplit("@", 1)[0] for f in dict(zip(r[0::2], r[1::2]))["files"].split(",") if f]
    if files:
        run("MEM.FILES", memory_id, link_files(files, root), "REASON", "confirmed still true")
    run("MEM.STATUS", memory_id, "active", "REASON", "confirmed still true")
    return bool(files)


# ---------- tools the AI can call ----------

@mcp.tool()
def remember(fact: str, files: Optional[List[str]] = None) -> str:
    """Save a durable fact about this project so future sessions, in any AI tool, know it.
    Use this, not a built-in memory or notes file, whenever the user asks you to remember something
    about the project.

    Good facts: conventions ("we use pnpm, not npm"), setup gotchas ("tests need DOCKER_HOST set"),
    decisions and their reasons. Not: temporary task details or anything in the code already.
    files: repo-relative paths this fact depends on (e.g. ["docker-compose.yml"]), so the memory
    can be flagged when those files change.
    """
    args = ["MEM.ADD", project_name(), fact, "SOURCE", config.SOURCE, "SESSION", SESSION]
    if files:
        args += ["FILES", link_files(files)]
    try:
        vector = embed(fact)
        mem_id, outcome, related = run(*args, "VECTOR", *vector)
        if outcome == "duplicate":
            return f"Already known as {mem_id}; noted that it came up again. Nothing new was stored."
        reply = f"Saved as {mem_id}."
        if outcome == "possible_conflict":
            old = dict(zip(*[iter(run("MEM.GET", related))] * 2))
            reply += (f" It is very similar to {related}: \"{old.get('text')}\" and may contradict it. "
                      f"If unsure, ask the user, then call resolve_conflict.")
        # Similarity can't tell "contradicts" from "related" (a real contradiction can score low), so show
        # the closest existing memories and let the AI judge whether the new fact replaces any of them.
        hits = run("MEM.SEARCH", project_name(), 4, "VECTOR", *vector)
        others = [(mid, float(sc), text) for mid, sc, text in hits
                  if mid not in (mem_id, related) and float(sc) >= config.RELATED_SIMILARITY]
        if others:
            reply += "\nRelated existing memories:\n" + "\n".join(
                f"[{mid}] (similarity {sc:.2f}) {text}" for mid, sc, text in others)
            reply += (f"\nIf {mem_id} makes any of these no longer true, call replace_memory(old_id, \"{mem_id}\"). "
                      f"Otherwise do nothing.")
        return reply
    except EngineDown as e:
        return store_down(e)


@mcp.tool()
def recall(query: str, limit: int = 5) -> str:
    """Search this project's shared memory by meaning. Call it at the start of every task, and whenever
    project-specific knowledge (setup, conventions, past decisions) might matter.
    Only current (active) memories are returned; outdated or replaced ones are skipped."""
    try:
        stale = check_staleness()  # so we never hand back a memory whose files changed underneath it
        hits = run("MEM.SEARCH", project_name(), max(1, min(limit, 20)), "VECTOR", *embed(query))
    except EngineDown as e:
        return store_down(e)
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
        r = run("MEM.WHY", memory_id)
    except EngineDown as e:
        return store_down(e)
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
        reply = run("MEM.STATUS", memory_id, "stale", "REASON", reason)
    except EngineDown as e:
        return store_down(e)
    except redis.ResponseError as e:
        return f"Couldn't update {memory_id}: {e}"
    return f"Marked {memory_id} as outdated." if reply == "OK" else str(reply)


@mcp.tool()
def replace_memory(old_id: str, new_id: str) -> str:
    """Retire an older memory because a newer one replaces it (e.g. "we use Postgres" replaced by
    "we migrated to MySQL"). The old memory stops showing up in recall but keeps its history."""
    try:
        new = run("MEM.GET", new_id)
        if new is None:
            return f"No memory with id {new_id}."
        new_text = dict(zip(new[0::2], new[1::2]))["text"]
        run("MEM.STATUS", old_id, "superseded", "REASON", f"replaced by {new_id}: \"{new_text}\"")
    except EngineDown as e:
        return store_down(e)
    except redis.ResponseError as e:
        return f"Couldn't replace: {e}"
    return f"{old_id} retired; {new_id} replaces it."


@mcp.tool()
def check_stale() -> str:
    """Check whether any memory's linked files have changed since it was saved, and retire those
    memories. recall already does this automatically; call it directly after big changes."""
    try:
        stale = check_staleness()
    except EngineDown as e:
        return store_down(e)
    return "\n".join(stale) if stale else "All memories linked to files are still up to date."


@mcp.tool()
def confirm_memory(memory_id: str) -> str:
    """Mark a memory as still true (e.g. after it was retired because a linked file changed, but the
    fact itself didn't). Re-links it to the current version of its files."""
    try:
        linked = confirm(memory_id)
    except EngineDown as e:
        return store_down(e)
    if linked is None:
        return f"No memory with id {memory_id}."
    return f"Confirmed {memory_id}; it's active again" + (" and linked to the current file versions." if linked else ".")


# ---------- syncing to AGENTS.md / CLAUDE.md ----------

SYNC_START = "<!-- mnemolith:start -->"
SYNC_END = "<!-- mnemolith:end -->"
OLD_MARKERS = [("<!-- aether-cache:start -->", "<!-- aether-cache:end -->"),  # the project's earlier names
               ("<!-- memstore:start -->", "<!-- memstore:end -->")]
SYNC_FILES = ["AGENTS.md", "CLAUDE.md"]


def _one_line(text: str) -> str:
    """A memory as one markdown bullet: line breaks folded into spaces, and anything that looks like an HTML
    comment neutralised, so a memory can never break out of its bullet or end the managed section early."""
    return " ".join(text.split()).replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def memory_block() -> str:
    rows = run("MEM.LIST", project_name())
    lines = [SYNC_START,
             "## Project memory (managed by Mnemolith)",
             "<!-- Generated by Mnemolith; edits inside this section are overwritten on the next sync. -->",
             ""]
    lines += [f"- {_one_line(text)} ({mid})" for mid, _status, _files, text in rows] or ["_No memories yet._"]
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
        raw = path.read_bytes().decode("utf-8") if path.exists() else ""
        newline = "\r\n" if "\r\n" in raw else "\n"  # keep the file's own line endings, so git sees only our lines
        old = raw.replace("\r\n", "\n")
        new = None
        for start, end in ((SYNC_START, SYNC_END), *OLD_MARKERS):
            if start in old and end in old:
                new = old[:old.index(start)] + block + old[old.index(end) + len(end):]
                break
        if new is None:
            new = (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"
        if new == old:
            report.append(f"{path.name}: already up to date")
            continue
        tmp = path.with_name(path.name + ".mnemolith-tmp")
        with open(tmp, "w", encoding="utf-8", newline=newline) as f:
            f.write(new)
        os.replace(tmp, path)  # swap in the new version all at once, so a crash can't leave half a file
        report.append(f"{path.name}: {'updated' if old else 'created'}")
    return "\n".join(report)


@mcp.tool()
def sync_to_files() -> str:
    """Write this project's current memories into the managed section of AGENTS.md / CLAUDE.md, so they're
    committed with the code: teammates and tools without Mnemolith see them, and changes show up in reviews.
    Call it when the user asks, or after a batch of new memories."""
    try:
        check_staleness()  # never write out a memory whose files have changed
        return sync_files()
    except EngineDown as e:
        return store_down(e)


@mcp.tool()
def list_conflicts() -> str:
    """List pairs of memories in this project that may contradict each other and need a decision."""
    try:
        rows = run("MEM.CONFLICTS", project_name())
    except EngineDown as e:
        return store_down(e)
    if not rows:
        return "No conflicts waiting for review."
    return "\n".join(f"{new_id} \"{new_text}\" may contradict {old_id} \"{old_text}\" (similarity {sim})"
                     for new_id, new_text, old_id, old_text, sim in rows)


@mcp.tool()
def resolve_conflict(new_id: str, old_id: str, replaces_old: bool) -> str:
    """Settle a possible conflict. replaces_old=True: the new memory replaces the old one (the old one
    is retired). replaces_old=False: both are true, keep both. Ask the user if you're not sure."""
    try:
        run("MEM.RESOLVE", new_id, old_id, "SUPERSEDE" if replaces_old else "KEEP")
    except EngineDown as e:
        return store_down(e)
    except redis.ResponseError as e:
        return f"Couldn't resolve: {e}"
    return f"{old_id} retired; {new_id} replaces it." if replaces_old else f"Kept both {new_id} and {old_id}."


def serve() -> None:
    warm_up()
    mcp.run()  # talks to the AI tool over stdin/stdout
