"""
Finding and adding the git repos whose memories Aether Cache manages.

Adding a repo just records where it lives (aether:repo:<project> -> folder), which lets the dashboard check
its memories' files from anywhere. Nothing is read from the repo beyond git metadata; nothing leaves the machine.
"""
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from . import server

# Where people usually keep code. Only these are searched, a few levels deep, and only when asked.
SEARCH_ROOTS = ["Desktop", "Documents", "Projects", "projects", "Developer", "dev", "code", "Code", "src",
                "repos", "git", "GitHub", "workspace", "work", "Sites"]
SKIP = {"node_modules", "Library", "Applications", "venv", ".venv", "env", "__pycache__", "build", "dist", "target",
        "vendor", "Pods", "DerivedData", ".Trash", "Pictures", "Music", "Movies", "Downloads"}
MAX_DEPTH = 4
MAX_RESULTS = 300
TIME_BUDGET = 3.0  # seconds; a huge Documents folder shouldn't make the dashboard hang


class RepoError(Exception):
    pass


def git_top(path: Path) -> Optional[Path]:
    """The repo's top folder if path is inside a git repo, else None."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def github_remote(path: Path) -> Optional[str]:
    """"owner/repo" if the repo's origin is on GitHub (read from local git config, no network), else None."""
    try:
        out = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=path, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", out.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def discover() -> List[dict]:
    """Git repos in the usual code folders under your home folder, newest first."""
    home = Path.home()
    roots = [home / r for r in SEARCH_ROOTS if (home / r).is_dir()]
    found, seen = [], set()
    deadline = time.monotonic() + TIME_BUDGET

    def walk(folder: Path, depth: int) -> None:
        if depth > MAX_DEPTH or len(found) >= MAX_RESULTS or time.monotonic() > deadline:
            return
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return  # no permission (e.g. macOS-protected folders); skip quietly
        if any(e.name == ".git" for e in entries):
            real = folder.resolve()
            if real not in seen:
                seen.add(real)
                try:
                    modified = (folder / ".git").stat().st_mtime
                except OSError:
                    modified = 0
                found.append({"path": str(folder), "modified": modified})
            return  # don't look for repos inside a repo
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP:
                walk(Path(e.path), depth + 1)

    for root in roots:
        walk(root, 1)
    found.sort(key=lambda r: -r["modified"])
    for r in found:
        r["name"] = Path(r["path"]).name
        r["github"] = github_remote(Path(r["path"]))
    return found


def add(path: str) -> dict:
    """Adds a project by folder (for a git repo, any folder inside it works). Returns {name, path, github}."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise RepoError(f"{path} isn't a folder.")
    # A git repo is added by its top folder (so memories match from any subfolder). A plain folder works too:
    # file fingerprints don't need git, and the AI tools treat a non-git folder as a project named after it.
    top = git_top(folder) or folder.resolve()
    name = top.name
    existing = server.db().get(server.REPO_KEY + name)
    if existing and Path(existing).resolve() != top.resolve() and Path(existing).is_dir():
        raise RepoError(f"A different repo with the same folder name, {name}, is already added ({existing}). "
                        f"Projects are named after their folder, so rename one of the folders to keep both.")
    server.db().set(server.REPO_KEY + name, str(top))
    server.db().set(HIDDEN_KEY + name, "")  # adding a removed project back shows it again
    return {"name": name, "path": str(top), "github": github_remote(top)}


HIDDEN_KEY = "aether:removed:"  # aether:removed:<project> -> when it was removed from the dashboard (ms)


def remove(name: str) -> None:
    """Forgets where a repo lives and hides the project from the dashboard. Memories are never deleted."""
    import time
    server.db().set(server.REPO_KEY + name, "")  # the engine has no DEL; an empty value means "not set"
    server.db().set(HIDDEN_KEY + name, str(int(time.time() * 1000)))


def pick_folder() -> Optional[str]:
    """Opens this computer's own folder picker and returns the chosen path, or None if cancelled.
    Raises RepoError if there's no picker available (then the user can type a path instead)."""
    system = platform.system()
    if system == "Darwin":
        # "tell me to activate" brings the picker to the front without asking to control another app
        script = ('tell me to activate\n'
                  'POSIX path of (choose folder with prompt "Choose a project folder to add to Aether Cache")')
        cmd = ["osascript", "-e", script]
    elif system == "Windows":
        ps = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
              "$d.Description = 'Choose a git repo to add to Aether Cache'; "
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        cmd = ["powershell", "-NoProfile", "-Command", ps]
    else:
        cmd = ["zenity", "--file-selection", "--directory", "--title=Choose a git repo to add to Aether Cache"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise RepoError("This computer has no folder picker available. Paste the folder's path instead.")
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:  # cancelled
        return None
    chosen = out.stdout.strip()
    return (chosen.rstrip("/\\") or chosen) or None
