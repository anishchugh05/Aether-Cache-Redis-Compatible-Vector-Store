"""
Moving memories in and out of Aether Cache.

Import reads the notes people already keep for their AI tools (CLAUDE.md, AGENTS.md, Cursor rules, ...) and
turns each bullet or short paragraph into a memory, linked to any repo files it mentions, so imported memories
get staleness checks from day one. Export writes memories, with their full history, to a JSON file.
"""
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import __version__, server

# Notes files AI tools read, relative to the repo's top folder. Checked in this order; a file that is a
# symlink to one already read (CLAUDE.md -> AGENTS.md is common) is read once.
NOTE_FILES = ["CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "AGENTS.md", ".cursorrules",
              ".windsurfrules", ".github/copilot-instructions.md"]
NOTE_GLOBS = [".cursor/rules/*.mdc", ".cursor/rules/*.md", ".cursor/rules/**/*.mdc"]
MIN_NOTE, MAX_NOTE = 12, 1000  # characters; shorter is a fragment, longer is a document, not a fact
MAX_FILE_BYTES = 512_000
MAX_LINKS = 6

# Sections Aether Cache writes into these files itself. Importing them would copy our own output back in.
INSTR_START = "<!-- aether-cache:instructions:start -->"
INSTR_END = "<!-- aether-cache:instructions:end -->"
MANAGED = [(server.SYNC_START, server.SYNC_END), server.OLD_MARKERS, (INSTR_START, INSTR_END)]


class NotesError(Exception):
    pass


# ---------- finding notes ----------

def note_files(root: Path) -> List[Path]:
    seen, found = set(), []
    candidates = [root / n for n in NOTE_FILES] + [p for g in NOTE_GLOBS for p in sorted(root.glob(g))]
    for p in candidates:
        try:
            if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                continue
            real = p.resolve()
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            found.append(p)
    return found


def _strip_managed(text: str) -> str:
    for start, end in MANAGED:
        while start in text and end in text[text.index(start):]:
            i = text.index(start)
            j = text.index(end, i) + len(end)
            text = text[:i] + text[j:]
    return text


def _clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)  # bold markers
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)  # [text](url)
    text = re.sub(r"\s+", " ", text).strip()
    return text


BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def split_notes(text: str) -> List[str]:
    """Markdown -> one string per bullet or paragraph, each carrying the context it needs to stand alone:
    the section heading ("Testing: run make test"), or the line that introduced a nested list."""
    text = _strip_managed(text.replace("\r\n", "\n"))
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if text.startswith("---\n"):  # front matter (Cursor .mdc rules)
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:]

    notes: List[str] = []
    heading = ""
    intro: List[tuple] = []  # (indent, text) of lines ending in ":" that introduce the list below them
    para: List[str] = []
    bullet: Optional[dict] = None
    in_code = False

    def emit(body: str, context: str) -> None:
        body = _clean(body)
        context = context or heading
        if context and not body.lower().startswith(context.lower()):
            body = f"{context}: {body}"
        if MIN_NOTE <= len(body) <= MAX_NOTE and re.search(r"[A-Za-z]{3}", body):
            notes.append(body)

    def flush_para() -> None:
        nonlocal para
        if para:
            body = _clean(" ".join(para))
            if body.endswith(":"):
                intro[:] = [(-1, body[:-1])]
            else:
                emit(body, "")
            para = []

    def flush_bullet() -> None:
        nonlocal bullet
        if bullet:
            body = _clean(" ".join(bullet["lines"]))
            if body.endswith(":"):
                intro.append((bullet["indent"], body[:-1]))
            else:
                emit(body, bullet["context"])
            bullet = None

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush_para(); flush_bullet()
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEADING.match(line)
        if m:
            flush_para(); flush_bullet()
            intro.clear()
            heading = _clean(m.group(2)) if len(m.group(1)) >= 2 else ""
            continue
        if not stripped:
            flush_para(); flush_bullet()
            continue
        if stripped.startswith("|") or re.fullmatch(r"[-*_]{3,}", stripped):  # tables, horizontal rules
            flush_para(); flush_bullet()
            continue
        if stripped.startswith(">"):
            line = stripped.lstrip("> ")
            stripped = line.strip()
        b = BULLET.match(line)
        if b:
            flush_para(); flush_bullet()
            indent = len(b.group(1).expandtabs(4))
            while intro and intro[-1][0] >= indent:
                intro.pop()
            bullet = {"indent": indent, "lines": [b.group(2)], "context": intro[-1][1] if intro else ""}
            continue
        if bullet and line[:1] in (" ", "\t"):
            bullet["lines"].append(stripped)  # a bullet's wrapped continuation line
            continue
        flush_bullet()
        if not para:
            intro.clear()  # a new paragraph ends whatever list came before it
        para.append(stripped)
    flush_para(); flush_bullet()
    return notes


# ---------- linking notes to files ----------

TOKEN = re.compile(r"`([^`\n]+)`|([\w./~-]+)")


def mentioned_paths(note: str, root: Path, skip: Iterable[str] = ()) -> tuple:
    """(files in the repo the note mentions, path-like mentions that don't exist)."""
    linked, missing = [], []
    skip = set(skip)
    root_real = root.resolve()
    for m in TOKEN.finditer(note):
        raw = (m.group(1) or m.group(2)).strip()
        for cand in raw.split():  # `make -C src/app test` -> check each word
            cand = cand.strip(".,:;!?()[]{}'\"").removeprefix("./")
            if not cand or "://" in cand or cand.startswith(("-", "~", "/", "$")) or len(cand) > 200:
                continue
            if "." not in cand.rsplit("/", 1)[-1] and "/" not in cand:
                continue  # a plain word, not a file
            p = root / cand
            try:
                is_file = p.is_file() and p.resolve().is_relative_to(root_real)
            except (OSError, ValueError):
                is_file = False
            if is_file:
                rel = p.resolve().relative_to(root_real).as_posix()
                if rel not in skip and rel not in linked:
                    linked.append(rel)
            elif "/" in cand and re.search(r"\.[A-Za-z0-9]{1,8}$", cand) and cand not in missing:
                missing.append(cand)  # looks like a path to a file, but there's no such file
    return linked[:MAX_LINKS], missing


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip().rstrip(".")


def _key(file: str, text: str) -> str:
    return hashlib.sha1(f"{file}\n{text}".encode()).hexdigest()[:12]


def scan(project: str, root: Path) -> dict:
    """Everything import would bring in from this repo's notes files, tagged for review:
    known (already remembered), missing (mentions a file that doesn't exist, so probably already stale)."""
    if not root.is_dir():
        raise NotesError(f"{root} isn't a folder.")
    existing = set()
    for _mid, _status, _files, text in server.run("MEM.LIST", project, "STATUS", "any"):
        existing.add(_norm(text))
    files = note_files(root)
    rels = [p.relative_to(root).as_posix() for p in files]
    notes, seen, per_file = [], set(), []
    for path, rel in zip(files, rels):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count = 0
        for note in split_notes(text):
            n, body = _norm(note), _norm(note.split(": ", 1)[-1])  # with and without its section heading
            if n in seen or body in seen:
                continue  # the same note twice, e.g. in CLAUDE.md and in a Cursor rule
            seen.update((n, body))
            linked, missing = mentioned_paths(note, root, skip=rels)
            known = n in existing or body in existing
            notes.append({"key": _key(rel, note), "text": note, "file": rel, "files": linked,
                          "missing": missing, "known": known,
                          "selected": not known and not missing})
            count += 1
        per_file.append({"path": rel, "notes": count})
    return {"project": project, "root": str(root), "files": per_file, "notes": notes}


def apply(project: str, root: Path, keys: Optional[List[str]] = None) -> dict:
    """Saves the chosen notes as memories (by key from scan; None means the ones selected by default).
    Notes that turn out to be already known are skipped, never duplicated."""
    found = scan(project, root)["notes"]
    chosen = [n for n in found if not n["known"] and (n["selected"] if keys is None else n["key"] in set(keys))]
    result = {"saved": [], "duplicates": 0, "conflicts": 0}
    if not chosen:
        return result
    vectors = server.embed_many([n["text"] for n in chosen])
    for note, vector in zip(chosen, vectors):
        args = ["MEM.ADD", project, note["text"], "SOURCE", f"{note['file']} (imported)", "SESSION", "import"]
        if note["files"]:
            args += ["FILES", server.link_files(note["files"], root)]
        mem_id, outcome, _related = server.run(*args, "VECTOR", *vector)
        if outcome == "duplicate":
            result["duplicates"] += 1
            continue
        result["saved"].append(mem_id)
        if outcome == "possible_conflict":
            result["conflicts"] += 1
    return result


# ---------- export ----------

def _iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).astimezone().isoformat(timespec="seconds")


def export(projects: Optional[List[str]] = None) -> dict:
    """Every memory (or those of the given projects), with linked files and full history."""
    from .ui import FILE_CHANGE, _files, all_memories, repo_locations  # the dashboard's readers
    wanted = set(projects) if projects else None
    mems = [m for m in all_memories() if wanted is None or m["project"] in wanted]
    pipe = server.db().pipeline(transaction=False)
    for m in mems:
        pipe.execute_command("MEM.WHY", m["id"])
    whys = pipe.execute() if mems else []
    repos = repo_locations()
    by_project: Dict[str, list] = {}
    for m, why in zip(mems, whys):
        d = dict(zip(why[0::2], why[1::2])) if why else {"history": []}
        history = d.get("history", [])
        status = {"superseded": "replaced"}.get(m["status"], m["status"])
        if status == "stale":
            last = next((det for _t, e, det in reversed(history) if "->" in e), "")
            status = "stale" if last.endswith(FILE_CHANGE) else "retired"
        by_project.setdefault(m["project"], []).append({
            "id": m["id"], "text": m["text"], "status": status, "source": m["source"],
            "session": d.get("session", ""), "saved_at": _iso(m["created_at"]),
            "files": _files(m["files"]),
            "history": [{"at": _iso(t), "event": e, "details": det} for t, e, det in history],
        })
    names = sorted(set(by_project) | ({p for p in (wanted or ())}), key=str.lower)
    return {
        "format": "aether-cache-export", "format_version": 1, "aether_cache_version": __version__,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "projects": [{"name": n, "repo": repos.get(n), "memories": sorted(
            by_project.get(n, []), key=lambda x: int(x["id"][1:]))} for n in names],
    }


def export_filename(project: Optional[str] = None) -> str:
    safe = re.sub(r"[^\w.-]+", "-", project).strip("-") if project else ""
    return f"aether-cache-{safe + '-' if safe else ''}{datetime.now():%Y-%m-%d}.json"
