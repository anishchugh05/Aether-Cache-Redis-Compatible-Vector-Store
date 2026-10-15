"""Where Aether Cache keeps things, and how to reach the engine. All settings are optional."""
import os
from pathlib import Path


def _env(*names: str, default: str = "") -> str:
    """First set environment variable among names (newer AETHER_* names win over older MEMORY_* ones)."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


# Everything lives under one folder: the engine's data, its log, and the downloaded embedding model.
HOME = Path(_env("AETHER_HOME", default=str(Path.home() / ".aether-cache"))).expanduser()
DATA_DIR = HOME / "data"
LOG_FILE = HOME / "engine.log"
PID_FILE = HOME / "engine.pid"
MODELS_DIR = HOME / "models"

# The engine listens on this machine only. The port is deliberately not Redis's 6379, so Aether Cache
# never collides with a real Redis you may already run.
HOST = _env("AETHER_HOST", "MEMORY_HOST", default="127.0.0.1")
PORT = int(_env("AETHER_PORT", "MEMORY_PORT", default="7380"))

# Which AI tool this server is running for (recorded with every memory it saves).
SOURCE = _env("AETHER_SOURCE", "MEMORY_SOURCE", default="unknown")
MODEL = _env("AETHER_MODEL", "MEMORY_MODEL", default="sentence-transformers/all-MiniLM-L6-v2")
FAKE_EMBEDDER = _env("AETHER_EMBEDDER", "MEMORY_EMBEDDER") == "fake"  # testing only, needs no download
# Existing memories at least this similar to a newly saved one are shown to the AI to judge.
# Low on purpose: in calibration, "we use Postgres" vs "we migrated to MySQL" scored only 0.30.
RELATED_SIMILARITY = float(_env("AETHER_RELATED_SIMILARITY", "MEMORY_RELATED_SIMILARITY", default="0.25"))


def project_override() -> str:
    return _env("AETHER_PROJECT", "MEMORY_PROJECT")


def repo_override() -> str:
    """The project folder, when the AI tool says where it is (Cursor passes ${workspaceFolder}).
    Ignored if the tool didn't fill in the placeholder."""
    value = _env("AETHER_REPO", "MEMORY_REPO")
    return "" if "${" in value else value
