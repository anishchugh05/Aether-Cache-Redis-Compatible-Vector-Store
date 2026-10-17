"""Registering Aether Cache with AI coding tools, so they start its MCP server on their own."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

NAME = "aether-cache"
TOOLS = ["claude-code", "cursor", "codex"]


def settings_env() -> dict:
    """Non-default settings from this shell (a custom port or data folder) that the tools must use too."""
    return {k: os.environ[k] for k in ("AETHER_PORT", "AETHER_HOME") if os.environ.get(k)}


def server_command() -> List[str]:
    """How a tool launches the server: this exact Python, so it works even when the tool's PATH
    doesn't include the folder the `aether-cache` command was installed into."""
    return [sys.executable, "-m", "aether_cache", "serve"]


def cursor_config() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"


def detected() -> List[str]:
    """The supported tools that look installed on this machine."""
    found = []
    if shutil.which("claude"):
        found.append("claude-code")
    if (Path.home() / ".cursor").is_dir():
        found.append("cursor")
    if shutil.which("codex") or codex_config().parent.is_dir():
        found.append("codex")
    return found


# ---------- instruction files ----------

INSTR_START = "<!-- aether-cache:instructions:start -->"
INSTR_END = "<!-- aether-cache:instructions:end -->"


def add_instructions(path: Path) -> str:
    """Adds (or refreshes) a short, clearly marked section telling the AI to use Aether Cache for project
    memory. Without it, tools tend to reach for their built-in memory instead. Nothing else is touched."""
    from .server import INSTRUCTIONS
    block = f"{INSTR_START}\n## Project memory\n{INSTRUCTIONS}\n{INSTR_END}"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if INSTR_START in old and INSTR_END in old:
        new = old[:old.index(INSTR_START)] + block + old[old.index(INSTR_END) + len(INSTR_END):]
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".aether-tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
    return f"told it to use Aether Cache for project memory in {path}"


def remove_instructions(path: Path) -> bool:
    """Takes our section back out of an instruction file, leaving the rest exactly as it was.
    Deletes the file only if our section was all that was in it. Returns whether anything changed."""
    try:
        old = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if INSTR_START not in old or INSTR_END not in old:
        return False
    before, after = old[:old.index(INSTR_START)], old[old.index(INSTR_END) + len(INSTR_END):]
    new = before.rstrip() + ("\n\n" + after.lstrip("\n") if before.strip() and after.strip() else after.lstrip("\n"))
    if not new.strip():
        path.unlink()
        return True
    if not new.endswith("\n"):
        new += "\n"
    tmp = path.with_name(path.name + ".aether-tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return True


def claude_instructions_file() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "CLAUDE.md"


# ---------- Claude Code ----------

def _claude(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([shutil.which("claude") or "claude", *args], capture_output=True, text=True, timeout=60)


def connect_claude_code() -> Tuple[bool, str]:
    if not shutil.which("claude"):
        return False, "Claude Code isn't installed (no `claude` command found)."
    notes = []
    # replace any earlier registration, including the one from when the project was called memstore
    for old in (NAME, "memstore"):
        if _claude("mcp", "get", old).returncode == 0:
            for scope in ("user", "local"):
                if _claude("mcp", "remove", old, "--scope", scope).returncode == 0 and old == "memstore":
                    notes.append("removed the old `memstore` entry")
    cmd = server_command()
    env_flags = [x for k, v in settings_env().items() for x in ("-e", f"{k}={v}")]
    out = _claude("mcp", "add", NAME, "--scope", "user", "-e", "MEMORY_SOURCE=claude-code", *env_flags, "--", *cmd)
    if out.returncode != 0:
        return False, f"`claude mcp add` failed: {(out.stderr or out.stdout).strip()}"
    notes.append(add_instructions(claude_instructions_file()))
    return True, "Claude Code: connected for all projects (" + "; ".join(notes) + ")."


def disconnect_claude_code() -> Tuple[bool, str]:
    notes = []
    if shutil.which("claude"):
        removed = [scope for scope in ("user", "local")
                   if _claude("mcp", "remove", NAME, "--scope", scope).returncode == 0]
        if removed:
            notes.append("removed the MCP server")
        if _claude("mcp", "get", NAME).returncode == 0:
            return False, "Claude Code: couldn't remove it. Run `claude mcp remove aether-cache` yourself."
    if remove_instructions(claude_instructions_file()):
        notes.append(f"removed our section from {claude_instructions_file()}")
    return True, "Claude Code: disconnected" + (f" ({'; '.join(notes)})." if notes else " (it wasn't connected).")


def claude_code_connected() -> bool:
    return bool(shutil.which("claude")) and _claude("mcp", "get", NAME).returncode == 0


# ---------- Cursor ----------

def _server_entry(source: str, workspace_var: str = "") -> dict:
    cmd = server_command()
    env = {"MEMORY_SOURCE": source, **settings_env()}
    if workspace_var:
        env["AETHER_REPO"] = workspace_var  # tells the server which project folder is open
    return {"command": cmd[0], "args": cmd[1:], "env": env}


def connect_cursor() -> Tuple[bool, str]:
    path = cursor_config()
    data = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            return False, (f"Couldn't read {path} ({e}). Fix it, or add this under \"mcpServers\" yourself:\n"
                           + json.dumps({NAME: _server_entry("cursor", "${workspaceFolder}")}, indent=2))
        shutil.copyfile(path, path.with_name("mcp.json.bak"))
    data.setdefault("mcpServers", {})[NAME] = _server_entry("cursor", "${workspaceFolder}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("mcp.json.aether-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True, f"Cursor: connected for all projects ({path}). Restart Cursor to pick it up."


def disconnect_cursor() -> Tuple[bool, str]:
    path = cursor_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except OSError:
        return True, "Cursor: disconnected (it wasn't connected)."
    except json.JSONDecodeError as e:
        return False, f"Cursor: couldn't read {path} ({e}). Remove the \"{NAME}\" entry yourself."
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or NAME not in servers:
        return True, "Cursor: disconnected (it wasn't connected)."
    shutil.copyfile(path, path.with_name("mcp.json.bak"))
    del servers[NAME]
    tmp = path.with_name("mcp.json.aether-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True, f"Cursor: disconnected ({path}). Restart Cursor to pick it up."


def cursor_connected() -> bool:
    try:
        return NAME in json.loads(cursor_config().read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        return False


# ---------- Codex ----------

def _codex_block() -> str:
    cmd = server_command()
    q = json.dumps  # TOML basic strings use the same escaping as JSON strings
    return (f"[mcp_servers.{NAME}]\n"
            f"command = {q(cmd[0])}\n"
            f"args = [{', '.join(q(a) for a in cmd[1:])}]\n"
            "env = { " + ", ".join(f"{k} = {q(v)}" for k, v in {"MEMORY_SOURCE": "codex", **settings_env()}.items())
            + " }\n")


def _is_our_header(line: str) -> bool:
    s = line.strip().replace('"', "").replace("'", "")
    return s == f"[mcp_servers.{NAME}]" or s.startswith(f"[mcp_servers.{NAME}.")


def connect_codex() -> Tuple[bool, str]:
    path = codex_config()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    body = _without_our_section(text)  # drop our old section (if any): from its header to the next table header
    new = (body + "\n\n" if body else "") + _codex_block()
    if path.exists():
        shutil.copyfile(path, path.with_name("config.toml.bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("config.toml.aether-tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    note = add_instructions(path.with_name("AGENTS.md"))  # Codex's instructions for every project
    return True, f"Codex: connected ({path}; {note})."


def _without_our_section(text: str) -> str:
    kept, skipping = [], False
    for line in text.splitlines():
        if line.strip().startswith("["):
            skipping = _is_our_header(line)
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip()


def disconnect_codex() -> Tuple[bool, str]:
    path = codex_config()
    notes = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if any(_is_our_header(l) for l in text.splitlines()):
        shutil.copyfile(path, path.with_name("config.toml.bak"))
        body = _without_our_section(text)
        tmp = path.with_name("config.toml.aether-tmp")
        tmp.write_text(body + "\n" if body else "", encoding="utf-8")
        os.replace(tmp, path)
        notes.append(f"removed the MCP server from {path}")
    if remove_instructions(path.with_name("AGENTS.md")):
        notes.append(f"removed our section from {path.with_name('AGENTS.md')}")
    return True, "Codex: disconnected" + (f" ({'; '.join(notes)})." if notes else " (it wasn't connected).")


def codex_connected() -> bool:
    try:
        return any(_is_our_header(l) for l in codex_config().read_text(encoding="utf-8").splitlines())
    except OSError:
        return False


# ---------- any other MCP tool ----------

def generic_instructions() -> str:
    entry = _server_entry("your-tool-name")
    return ("For any other MCP-capable tool, add this server to its MCP config (most use this JSON shape):\n\n"
            + json.dumps({"mcpServers": {NAME: entry}}, indent=2)
            + "\n\nOr, as a single command: " + " ".join(f'"{c}"' if " " in c else c for c in server_command())
            + "\nRun the tool from your project folder, or set AETHER_REPO to the project's path.")


CONNECTORS = {"claude-code": connect_claude_code, "cursor": connect_cursor, "codex": connect_codex}
DISCONNECTORS = {"claude-code": disconnect_claude_code, "cursor": disconnect_cursor, "codex": disconnect_codex}
LABELS = {"claude-code": "Claude Code", "cursor": "Cursor", "codex": "Codex"}
CHECKS = {"claude-code": claude_code_connected, "cursor": cursor_connected, "codex": codex_connected}
