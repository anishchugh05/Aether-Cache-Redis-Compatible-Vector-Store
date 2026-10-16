"""
`aether-cache ui`: a local dashboard for seeing and fixing what your AI tools remember.

A small HTTP server on this machine serves one page (ui/index.html) and a JSON API it calls.
Only this machine can reach it, and every API request must carry a secret token that's created fresh
each time the dashboard starts. That stops other websites open in your browser from reading or
changing your memories through it.
"""
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import redis

from . import __version__, config, engine, server

PAGE = Path(__file__).resolve().parent / "ui" / "index.html"
FONTS = {f.name: f for f in (PAGE.parent / "fonts").glob("*.woff2")}  # only these files are ever served
DEFAULT_PORT = int(os.environ.get("AETHER_UI_PORT", "7381"))
STATUSES = ("active", "stale", "superseded")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------- reading memories ----------

def _pairs(reply) -> dict:
    return dict(zip(reply[0::2], reply[1::2]))


def _files(files: str) -> list:
    """"a.py@<hash>,b.yml@<hash>" -> [{"path": "a.py", "hash": "<hash>"}, ...]"""
    out = []
    for f in files.split(","):
        if f:
            path, _, h = f.rpartition("@") if "@" in f else (f, "", "")
            out.append({"path": path, "hash": h})
    return out


def all_memories() -> list:
    """Every memory, read in one batch. The engine numbers memories m1, m2, ... and never deletes them,
    so reading ids in order until a run of missing ones finds them all."""
    found, start, batch = [], 1, 200
    while True:
        pipe = server.db().pipeline(transaction=False)
        for n in range(start, start + batch):
            pipe.execute_command("MEM.GET", f"m{n}")
        replies = pipe.execute()
        for r in replies:
            if r is not None:
                found.append(_pairs(r))
        if all(r is None for r in replies[-50:]):
            return found
        start += batch


def repo_locations() -> dict:
    """project -> repo folder, as recorded by the MCP server when an AI used Aether Cache there."""
    keys = [k for k in server.db().keys("*") if k.startswith(server.REPO_KEY)]
    pipe = server.db().pipeline(transaction=False)  # one round trip; the engine has no MGET
    for k in keys:
        pipe.get(k)
    values = pipe.execute() if keys else []
    return {k[len(server.REPO_KEY):]: v for k, v in zip(keys, values) if v}


def repo_for(project: str) -> Optional[Path]:
    path = server.db().get(server.REPO_KEY + project)
    return Path(path) if path and Path(path).is_dir() else None


def conflicts(project: str) -> list:
    rows = server.run("MEM.CONFLICTS", project)
    return [{"new_id": n, "new_text": nt, "old_id": o, "old_text": ot, "similarity": float(sim)}
            for n, nt, o, ot, sim in rows]


def last_reason(memory_id: str) -> str:
    """Why a memory has its current status, from the latest status change in its history."""
    why = _pairs(server.run("MEM.WHY", memory_id) or [])
    for _t, event, details in reversed(why.get("history", [])):
        if "->" in event:
            return details
    return ""


# The engine has one "stale" status for two different situations: the file check found a linked file changed
# (needs a decision), or someone retired the memory on purpose (already decided). The reason tells them apart.
FILE_CHANGE = "since this was saved"


def memory_summary(m: dict) -> dict:
    out = {"id": m["id"], "text": m["text"], "status": m["status"], "source": m["source"],
           "created_at": int(m["created_at"]), "files": _files(m["files"])}
    if m["status"] == "stale":
        out["reason"] = last_reason(m["id"])
        out["retired"] = not out["reason"].endswith(FILE_CHANGE)
    return out


# ---------- API ----------

def api_projects(_q, _body) -> dict:
    repos = repo_locations()
    projects = {}
    for m in all_memories():
        p = projects.setdefault(m["project"], {"name": m["project"], "counts": dict.fromkeys(STATUSES + ("retired",), 0)})
        status = m["status"]
        if status == "stale" and not last_reason(m["id"]).endswith(FILE_CHANGE):
            status = "retired"
        p["counts"][status] += 1
    for name, p in projects.items():
        repo = repos.get(name)
        p["repo"] = repo if repo and Path(repo).is_dir() else None
    order = sorted(projects.values(), key=lambda p: (-p["counts"]["stale"], p["name"].lower()))
    return {"projects": order, "version": __version__}


def api_memories(q, _body) -> dict:
    project = _need(q, "project")
    status = q.get("status", "any")
    if status not in STATUSES + ("retired", "any"):
        raise ApiError(400, "status must be active, stale, retired, superseded or any")
    query = q.get("q", "").strip()
    by_id = {m["id"]: m for m in all_memories() if m["project"] == project}
    engine_status = "stale" if status == "retired" else status
    if query:
        hits = server.run("MEM.SEARCH", project, 50, "STATUS", engine_status, "VECTOR", *server.embed(query))
        items = [dict(memory_summary(by_id[mid]), score=round(float(sc), 3)) for mid, sc, _t in hits if mid in by_id]
    else:
        items = [memory_summary(m) for m in by_id.values() if engine_status == "any" or m["status"] == engine_status]
        items.sort(key=lambda m: -m["created_at"])
    if status in ("stale", "retired"):  # split the engine's one "stale" into its two meanings
        items = [m for m in items if m.get("retired") == (status == "retired")]
    return {"memories": items}


def api_attention(q, _body) -> dict:
    """What needs a decision in this project: memories that went stale, and possible contradictions.
    Checks the project's files first (when its repo location is known), like recall does."""
    project = _need(q, "project")
    repo = repo_for(project)
    just_went_stale = server.check_staleness(project, repo) if repo else []
    stale = []
    for m in all_memories():
        if m["project"] == project and m["status"] == "stale":
            summary = memory_summary(m)
            if not summary["retired"]:
                if repo:  # show what each file's contents are now, next to what the memory was based on
                    now = server.content_hashes([f["path"] for f in summary["files"]], repo)
                    for f in summary["files"]:
                        f["now"] = now.get(f["path"])
                stale.append(summary)
    stale.sort(key=lambda m: -m["created_at"])
    return {"stale": stale, "conflicts": conflicts(project), "checked_files": repo is not None,
            "repo": str(repo) if repo else None, "just_went_stale": len(just_went_stale)}


def api_memory(q, _body) -> dict:
    why = server.run("MEM.WHY", _need(q, "id"))
    if why is None:
        raise ApiError(404, "No memory with that id.")
    d = _pairs(why)
    return {"id": d["id"], "text": d["text"], "project": d["project"], "status": d["status"],
            "source": d["source"], "session": d["session"], "created_at": int(d["created_at"]),
            "files": _files(d["files"]),
            "history": [{"at": int(t), "event": e, "details": det} for t, e, det in d["history"]],
            "retired": d["status"] == "stale" and not next(
                (det for _t, e, det in reversed(d["history"]) if "->" in e), "").endswith(FILE_CHANGE)}


def api_confirm(_q, body) -> dict:
    memory_id = _need(body, "id")
    got = server.run("MEM.GET", memory_id)
    if got is None:
        raise ApiError(404, "No memory with that id.")
    project = _pairs(got)["project"]
    repo = repo_for(project)
    if repo is None and _files(_pairs(got)["files"]):
        raise ApiError(409, "Can't re-link this memory's files from here: the dashboard doesn't know where the "
                            f"{project} repo is yet. Use your AI tool in that repo once, then try again.")
    server.confirm(memory_id, repo)
    return {"ok": True}


def api_retire(_q, body) -> dict:
    memory_id = _need(body, "id")
    reason = (body.get("reason") or "").strip() or "retired from the dashboard"
    try:
        server.run("MEM.STATUS", memory_id, "stale", "REASON", reason)
    except redis.ResponseError as e:
        raise ApiError(404, str(e))
    return {"ok": True}


def api_resolve(_q, body) -> dict:
    keep_both = bool(body.get("keep_both"))
    try:
        server.run("MEM.RESOLVE", _need(body, "new_id"), _need(body, "old_id"), "KEEP" if keep_both else "SUPERSEDE")
    except redis.ResponseError as e:
        raise ApiError(409, str(e))
    return {"ok": True}


def _need(d: dict, key: str) -> str:
    value = d.get(key)
    if not value or not isinstance(value, str):
        raise ApiError(400, f"Missing {key}.")
    return value


ROUTES = {
    ("GET", "/api/projects"): api_projects,
    ("GET", "/api/memories"): api_memories,
    ("GET", "/api/attention"): api_attention,
    ("GET", "/api/memory"): api_memory,
    ("POST", "/api/confirm"): api_confirm,
    ("POST", "/api/retire"): api_retire,
    ("POST", "/api/resolve"): api_resolve,
}


# ---------- HTTP ----------

def make_handler(token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "aether-cache"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src data:; font-src 'self'; base-uri 'none'; form-action 'none'; "
                             "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

        def _handle(self, method: str) -> None:
            # A website can point a domain it controls at 127.0.0.1 ("DNS rebinding"); the Host header gives it away
            if self.headers.get("Host") not in allowed_hosts:
                return self._json(403, {"error": "Forbidden host."})
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and url.path.startswith("/fonts/") and url.path[7:] in FONTS:
                return self._send(200, FONTS[url.path[7:]].read_bytes(), "font/woff2")
            route = ROUTES.get((method, url.path))
            if route is None:
                return self._json(404, {"error": "Not found."})
            if not secrets.compare_digest(self.headers.get("X-Aether-Token", ""), token):
                return self._json(401, {"error": "Missing or wrong token. Reopen the dashboard with `aether-cache ui`."})
            body = {}
            if method == "POST":
                # requiring JSON means a cross-site form can't post here without the browser asking first
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._json(415, {"error": "Send JSON."})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(min(length, 1_000_000)) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "Invalid JSON."})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "Invalid JSON."})
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                self._json(200, route(query, body))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except server.EngineDown as e:
                self._json(503, {"error": str(e)})
            except (redis.ConnectionError, redis.TimeoutError):
                self._json(503, {"error": "The engine stopped responding. Run `aether-cache start`."})
            except Exception as e:  # never crash the server over one request
                self._json(500, {"error": f"Something went wrong: {e}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def serve(port: Optional[int] = None, open_browser: bool = True) -> int:
    try:
        engine.ensure_running()
    except engine.EngineError as e:
        print(e)
        return 1
    token = secrets.token_urlsafe(24)
    port = port or DEFAULT_PORT
    httpd = None
    for candidate in range(port, port + 10):  # if the port is taken, try the next few
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(token, candidate))
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print(f"Couldn't find a free port between {port} and {port + 9}. Pass one with --port.")
        return 1
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/#token={token}"  # after #, so it's never sent in a request or logged
    print(f"Aether Cache dashboard: {url}\nPress Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    if not config.FAKE_EMBEDDER:
        threading.Thread(target=lambda: _quietly(server.embedder), daemon=True).start()  # makes first search fast
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        httpd.server_close()
    return 0


def _quietly(fn) -> None:
    try:
        fn()
    except Exception:
        pass
