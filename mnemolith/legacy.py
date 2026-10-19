"""
Moving people over from Aether Cache, this project's earlier name.

Runs automatically and only once: the first time Mnemolith starts on a computer that had Aether Cache, it moves
~/.aether-cache (memories, the engine's data, the downloaded model and Java) to ~/.mnemolith, and renames the few
things Aether Cache stored inside the engine. Nothing is lost and nothing needs doing by hand.
"""
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Optional

from . import config

OLD_HOME = Path.home() / ".aether-cache"
OLD_NAME = "aether-cache"  # what AI tools' settings called the server before the rename
OLD_KEY_PREFIX = "aether:"
NEW_KEY_PREFIX = "mnemolith:"


def _stop_old_engine() -> None:
    """The old install's engine may still be running from the old folder; stop it so the folder can move."""
    from . import engine
    try:
        pid = int((OLD_HOME / "engine.pid").read_text().strip())
    except (OSError, ValueError):
        return
    if engine.probe() != "ours" or not engine._alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)  # safe: every acknowledged write is already on disk
    except OSError:
        return
    for _ in range(50):
        if not engine._alive(pid):
            break
        time.sleep(0.1)
    (OLD_HOME / "engine.pid").unlink(missing_ok=True)


def migrate_home() -> Optional[str]:
    """Moves ~/.aether-cache to ~/.mnemolith once. Returns a sentence saying so, or None if there was nothing to do."""
    if config.CUSTOM_HOME or not OLD_HOME.is_dir() or config.HOME.exists():
        return None
    _stop_old_engine()
    try:
        os.replace(OLD_HOME, config.HOME)
        how = "Moved"
    except OSError:  # e.g. Windows, while an old AI session still has a file open: copy instead, keep the original
        shutil.copytree(OLD_HOME, config.HOME, dirs_exist_ok=True)
        how = "Copied"
    return f"Aether Cache is now Mnemolith. {how} your memories from {OLD_HOME} to {config.HOME}."


def migrate_keys(db) -> None:
    """Renames the repo locations and removed-project marks Aether Cache kept in the engine. Best effort."""
    try:
        for key in [k for k in db.keys("*") if k.startswith(OLD_KEY_PREFIX)]:
            value = db.get(key)
            new = NEW_KEY_PREFIX + key[len(OLD_KEY_PREFIX):]
            if value and not db.get(new):
                db.set(new, value)
            if value:
                db.set(key, "")  # the engine has no DEL; empty means "not set"
    except Exception:
        pass
