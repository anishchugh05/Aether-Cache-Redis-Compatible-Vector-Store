"""
Moving memories in and out of Mnemolith.

Import reads the notes people already keep for their AI tools (CLAUDE.md, AGENTS.md, Cursor rules, ...) and
turns each bullet or short paragraph into a memory, linked to any repo files it mentions, so imported memories
get staleness checks from day one. Export writes memories, with their full history, to a JSON file.
"""
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import __version__, connect, server

# Notes files AI tools read, relative to the repo's top folder. Checked in this order; a file that is a
# symlink to one already read (CLAUDE.md -> AGENTS.md is common) is read once.
NOTE_FILES = ["CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "AGENTS.md", ".cursorrules",
              ".windsurfrules", ".github/copilot-instructions.md"]
NOTE_GLOBS = [".cursor/rules/*.mdc", ".cursor/rules/*.md", ".cursor/rules/**/*.mdc"]
MIN_NOTE, MAX_NOTE = 12, 1000  # characters; shorter is a fragment, longer is a document, not a fact
MAX_FILE_BYTES = 512_000
MAX_LINKS = 6

# Sections Mnemolith writes into these files itself. Importing them would copy our own output back in.
INSTR_START = "<!-- mnemolith:instructions:start -->"
INSTR_END = "<!-- mnemolith:instructions:end -->"
MANAGED = [(server.SYNC_START, server.SYNC_END), *server.OLD_MARKERS, (INSTR_START, INSTR_END), *connect.OLD_INSTR]


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


BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
TODO = re.compile(r"(?i)^(todo|fixme|xxx|hack|wip)\b")
# Words and marks that make a line read like a fact about the project ("we use", "never", a `command`, a number)
SIGNAL = re.compile(r"(?i)`|\d|/|\b(always|never|must|should|don'?t|do not|use[sd]?|using|prefer|avoid|requires?|"
                    r"required|needs?|is|are|was|were|runs?|lives?|goes|stored?|keeps?|has|have|only|instead)\b")
# Lines about the notes file itself, or pleasantries, rather than about the project
META = re.compile(r"(?i)\b(read this|this (file|doc|document|guide|readme)|these (notes|instructions|rules|docs)|"
                  r"seriously|have fun|good luck|thank you|thanks)\b")


def _step(item: dict) -> str:
    """A numbered step plus anything nested under it: "Build: run uv build; check dist/"."""
    text = item["text"].rstrip(":").rstrip()
    details = [d.rstrip(".") for d in item["details"] if d and not TODO.match(d)]
    return f"{text}: {'; '.join(details)}" if details else text


def split_notes(text: str) -> List[dict]:
    """Markdown -> one note per bullet or paragraph, each carrying the context it needs to stand alone: the
    section heading ("Testing: run make test"), or the line that introduced a nested list. A numbered list of
    short steps is one procedure, so it becomes one note. TODOs and checklist items are skipped. Notes that
    don't read like a fact about the project are marked weak (offered, but not selected by default)."""
    text = _strip_managed(text.replace("\r\n", "\n"))
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if text.startswith("---\n"):  # front matter (Cursor .mdc rules)
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:]

    notes: List[dict] = []
    heading = ""
    intro: List[tuple] = []  # (indent, text) of lines ending in ":" that introduce the list below them
    para: List[str] = []
    bullet: Optional[dict] = None
    steps: Optional[dict] = None  # a numbered list being collected: {"indent", "context", "items"}
    in_code = False

    def emit(body: str, context: str) -> None:
        body = _clean(body)
        if not body or TODO.match(body):
            return
        context = context or heading
        if context and not body.lower().startswith(context.lower()):
            body = f"{context}: {body}"
        if MIN_NOTE <= len(body) <= MAX_NOTE and re.search(r"[A-Za-z]{3}", body):
            weak = bool(META.search(body)) or (not context and not SIGNAL.search(body))
            notes.append({"text": body, "weak": weak})

    def flush_steps() -> None:
        nonlocal steps
        if steps:
            items = [_step(i) for i in steps["items"]]
            items = [t for t in items if t and not TODO.match(t)]
            short = items and (min(map(len, items)) < 30 or sum(map(len, items)) / len(items) < 45)
            joined = " ".join(f"{i}. {t if t[-1] in '.!?' else t + '.'}" for i, t in enumerate(items, 1))
            if len(items) > 1 and short and len(joined) <= MAX_NOTE:
                emit(joined, steps["context"])  # short steps only make sense together
            else:
                for t in items:
                    emit(t, steps["context"])  # longer items are separate rules that happen to be numbered
            steps = None

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
        nonlocal bullet, steps
        if not bullet:
            return
        b, bullet = bullet, None
        if b["checkbox"]:
            return  # a checklist item is a task, not a fact
        body = _clean(" ".join(b["lines"]))
        if steps and b["indent"] > steps["indent"] and steps["items"]:
            steps["items"][-1]["details"].append(body)  # a detail nested under a step
            return
        if body.endswith(":") and not b["ordered"]:
            flush_steps()
            intro.append((b["indent"], body[:-1]))
        elif b["ordered"]:
            if steps and (steps["indent"] != b["indent"] or steps["context"] != b["context"]):
                flush_steps()
            if not steps:
                steps = {"indent": b["indent"], "context": b["context"], "items": []}
            steps["items"].append({"text": body, "details": []})
        else:
            if steps and b["indent"] <= steps["indent"]:
                flush_steps()
            emit(body, b["context"])

    def flush_all() -> None:
        flush_para(); flush_bullet(); flush_steps()

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush_para(); flush_bullet()  # a code block inside a numbered list doesn't end the list
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEADING.match(line)
        if m:
            flush_all()
            intro.clear()
            heading = _clean(m.group(2)) if len(m.group(1)) >= 2 else ""
            continue
        if not stripped:
            flush_para(); flush_bullet()  # numbered lists often have blank lines between steps
            continue
        if stripped.startswith("|") or re.fullmatch(r"[-*_]{3,}", stripped):  # tables, horizontal rules
            flush_all()
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
            bullet = {"indent": indent, "lines": [b.group(4)], "context": intro[-1][1] if intro else "",
                      "ordered": b.group(2)[0].isdigit(), "checkbox": bool(b.group(3))}
            continue
        if bullet and line[:1] in (" ", "\t"):
            bullet["lines"].append(stripped)  # a bullet's wrapped continuation line
            continue
        if steps and not bullet and line[:1] in (" ", "\t"):
            steps["items"][-1]["text"] += " " + stripped  # a step's continuation after a blank line
            continue
        flush_bullet(); flush_steps()
        if not para:
            intro.clear()  # a new paragraph ends whatever list came before it
        para.append(stripped)
    flush_all()
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


# Same thresholds as the engine (Main.java): at or above DUPLICATE it treats a new memory as the same one,
# at or above CONFLICT as a possible contradiction. A note scoring that close to a memory you already have
# would only be skipped or flagged after importing, so it's flagged before instead.
DUPLICATE_SIMILARITY = 0.97
CONFLICT_SIMILARITY = 0.80


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _flag_similar(project: str, notes: List[dict]) -> Dict[str, List[str]]:
    """Compares each new note by meaning with the project's current memories, and with the notes before it
    in this import. Marks near-copies as known and close matches as similar (unselected). Returns the
    notes' embeddings by key, so importing them doesn't compute them twice."""
    fresh = [n for n in notes if not n["known"]]
    if not fresh:
        return {}
    vectors = server.embed_many([n["text"] for n in fresh])
    pipe = server.db().pipeline(transaction=False)
    for v in vectors:
        pipe.execute_command("MEM.SEARCH", project, 1, "STATUS", "active", "VECTOR", *v)
    try:
        hits = pipe.execute()
    except Exception:
        hits = [server.run("MEM.SEARCH", project, 1, "STATUS", "active", "VECTOR", *v) for v in vectors]
    floats = [[float(x) for x in v] for v in vectors]
    for i, (note, hit) in enumerate(zip(fresh, hits)):
        if hit:
            mid, score, text = hit[0][0], float(hit[0][1]), hit[0][2]
            if score >= DUPLICATE_SIMILARITY:
                note["known"] = True
                note["selected"] = False
                continue
            if score >= CONFLICT_SIMILARITY:
                note["similar"] = {"id": mid, "text": text, "score": round(score, 2)}
                note["selected"] = False
                continue
        for j in range(i):  # two notes in this import that say the same thing, e.g. in CLAUDE.md and a rule
            other = fresh[j]
            if other["known"] or other.get("similar"):
                continue
            score = _cosine(floats[i], floats[j])
            if score >= CONFLICT_SIMILARITY:
                note["similar"] = {"id": None, "text": other["text"], "score": round(score, 2)}
                note["selected"] = False
                break
    return {n["key"]: v for n, v in zip(fresh, vectors)}


def _scan(project: str, root: Path) -> tuple:
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
        for found in split_notes(text):
            note = found["text"]
            n, body = _norm(note), _norm(note.split(": ", 1)[-1])  # with and without its section heading
            if n in seen or body in seen:
                continue  # the same note twice, e.g. in CLAUDE.md and in a Cursor rule
            seen.update((n, body))
            linked, missing = mentioned_paths(note, root, skip=rels)
            known = n in existing or body in existing
            notes.append({"key": _key(rel, note), "text": note, "file": rel, "files": linked,
                          "missing": missing, "known": known, "weak": found["weak"], "similar": None,
                          "selected": not known and not missing and not found["weak"]})
            count += 1
        per_file.append({"path": rel, "notes": count})
    vectors = _flag_similar(project, notes)
    return {"project": project, "root": str(root), "files": per_file, "notes": notes}, vectors


def scan(project: str, root: Path) -> dict:
    """Everything import would bring in from this repo's notes files, tagged for review: known (already
    remembered, word for word or nearly), similar (close in meaning to a memory you have, or to another note
    here), missing (mentions a file that doesn't exist, so probably already stale), weak (doesn't read like
    a fact about the project). Only notes with none of these are selected by default."""
    return _scan(project, root)[0]


def apply(project: str, root: Path, keys: Optional[List[str]] = None) -> dict:
    """Saves the chosen notes as memories (by key from scan; None means the ones selected by default).
    Notes that turn out to be already known are skipped, never duplicated."""
    found, vectors = _scan(project, root)
    wanted = set(keys or ())
    chosen = [n for n in found["notes"] if not n["known"] and (n["selected"] if keys is None else n["key"] in wanted)]
    result = {"saved": [], "duplicates": 0, "conflicts": 0}
    for note in chosen:
        args = ["MEM.ADD", project, note["text"], "SOURCE", f"{note['file']} (imported)", "SESSION", "import"]
        if note["files"]:
            args += ["FILES", server.link_files(note["files"], root)]
        mem_id, outcome, _related = server.run(*args, "VECTOR", *vectors[note["key"]])
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
        "format": "mnemolith-export", "format_version": 1, "mnemolith_version": __version__,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "projects": [{"name": n, "repo": repos.get(n), "memories": sorted(
            by_project.get(n, []), key=lambda x: int(x["id"][1:]))} for n in names],
    }


def export_filename(project: Optional[str] = None) -> str:
    safe = re.sub(r"[^\w.-]+", "-", project).strip("-") if project else ""
    return f"mnemolith-{safe + '-' if safe else ''}{datetime.now():%Y-%m-%d}.json"
