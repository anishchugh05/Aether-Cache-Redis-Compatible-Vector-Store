"""
Kept so setups made before the `aether-cache` command existed (`python memory_mcp/server.py`) keep working.
The server now lives in the aether_cache package; new setups use `aether-cache setup` instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root, where aether_cache/ lives

from aether_cache.cli import main  # noqa: E402

main(sys.argv[1:] or ["serve"])  # `python server.py sync` and `python server.py check` still work too
