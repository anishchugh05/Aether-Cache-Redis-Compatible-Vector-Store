"""
Kept so setups made before the `mnemolith` command existed (`python memory_mcp/server.py`) keep working.
The server now lives in the mnemolith package; new setups use `mnemolith setup` instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root, where mnemolith/ lives

from mnemolith.cli import main  # noqa: E402

main(sys.argv[1:] or ["serve"])  # `python server.py sync` and `python server.py check` still work too
