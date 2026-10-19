"""Where Mnemolith keeps things, and how to reach the engine. All settings are optional."""
import os
from pathlib import Path


def _env(*names: str, default: str = "") -> str:
    """First set environment variable among names. Each MNEMOLITH_* setting also accepts the names from before
    the rename (AETHER_*, and MEMORY_* from before that), so existing setups keep working."""
    names = [n for name in names for n in (name, name.replace("MNEMOLITH_", "AETHER_", 1))]
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


# Everything lives under one folder: the engine's data, its log, and the downloaded embedding model.
HOME = Path(_env("MNEMOLITH_HOME", default=str(Path.home() / ".mnemolith"))).expanduser()
CUSTOM_HOME = bool(_env("MNEMOLITH_HOME"))
DATA_DIR = HOME / "data"
LOG_FILE = HOME / "engine.log"
PID_FILE = HOME / "engine.pid"
STAMP_FILE = HOME / "engine.version"  # which engine file the running engine was started from
MODELS_DIR = HOME / "models"

# Where people can reach us when something goes wrong (the website's contact form).
CONTACT_URL = "https://mnemolith.dev/install.html#contact"

# The engine listens on this machine only. The port is deliberately not Redis's 6379, so Mnemolith
# never collides with a real Redis you may already run.
HOST = _env("MNEMOLITH_HOST", "MEMORY_HOST", default="127.0.0.1")
PORT = int(_env("MNEMOLITH_PORT", "MEMORY_PORT", default="7380"))

# Which AI tool this server is running for (recorded with every memory it saves).
SOURCE = _env("MNEMOLITH_SOURCE", "MEMORY_SOURCE", default="unknown")
MODEL = _env("MNEMOLITH_MODEL", "MEMORY_MODEL", default="sentence-transformers/all-MiniLM-L6-v2")
FAKE_EMBEDDER = _env("MNEMOLITH_EMBEDDER", "MEMORY_EMBEDDER") == "fake"  # testing only, needs no download
# Existing memories at least this similar to a newly saved one are shown to the AI to judge.
# Low on purpose: in calibration, "we use Postgres" vs "we migrated to MySQL" scored only 0.30.
RELATED_SIMILARITY = float(_env("MNEMOLITH_RELATED_SIMILARITY", "MEMORY_RELATED_SIMILARITY", default="0.25"))


def project_override() -> str:
    return _env("MNEMOLITH_PROJECT", "MEMORY_PROJECT")


def repo_override() -> str:
    """The project folder, when the AI tool says where it is (Cursor passes ${workspaceFolder}).
    Ignored if the tool didn't fill in the placeholder."""
    value = _env("MNEMOLITH_REPO", "MEMORY_REPO")
    return "" if "${" in value else value
